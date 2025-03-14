import datetime
import gc
import os
import sys
from datetime import timedelta

import dask
import dask.array as da
import numpy as np
import psutil
import pytest
import xarray as xr

from parcels import (
    AdvectionRK4,
    AdvectionRK4_3D,
    FieldSet,
    JITParticle,
    ParticleSet,
    RectilinearZGrid,
    ScipyParticle,
    TimeExtrapolationError,
    Variable,
)
from parcels.field import Field, VectorField
from parcels.fieldfilebuffer import DaskFileBuffer
from parcels.tools.converters import (
    GeographicPolar,
    TimeConverter,
    UnitConverter,
)
from tests.common_kernels import DoNothing
from tests.utils import TEST_DATA

ptype = {"scipy": ScipyParticle, "jit": JITParticle}


def generate_fieldset_data(xdim, ydim, zdim=1, tdim=1):
    lon = np.linspace(0.0, 10.0, xdim, dtype=np.float32)
    lat = np.linspace(0.0, 10.0, ydim, dtype=np.float32)
    depth = np.zeros(zdim, dtype=np.float32)
    time = np.zeros(tdim, dtype=np.float64)
    if zdim == 1 and tdim == 1:
        U, V = np.meshgrid(lon, lat)
        dimensions = {"lat": lat, "lon": lon}
    else:
        U = np.ones((tdim, zdim, ydim, xdim))
        V = np.ones((tdim, zdim, ydim, xdim))
        dimensions = {"lat": lat, "lon": lon, "depth": depth, "time": time}
    data = {"U": np.array(U, dtype=np.float32), "V": np.array(V, dtype=np.float32)}

    return (data, dimensions)


@pytest.mark.parametrize("xdim", [100, 200])
@pytest.mark.parametrize("ydim", [100, 200])
def test_fieldset_from_data(xdim, ydim):
    """Simple test for fieldset initialisation from data."""
    data, dimensions = generate_fieldset_data(xdim, ydim)
    fieldset = FieldSet.from_data(data, dimensions)
    assert fieldset.U._creation_log == "from_data"
    assert len(fieldset.U.data.shape) == 3
    assert len(fieldset.V.data.shape) == 3
    assert np.allclose(fieldset.U.data[0, :], data["U"], rtol=1e-12)
    assert np.allclose(fieldset.V.data[0, :], data["V"], rtol=1e-12)


def test_fieldset_extra_syntax():
    """Simple test for fieldset initialisation from data."""
    data, dimensions = generate_fieldset_data(10, 10)

    with pytest.raises(SyntaxError):
        FieldSet.from_data(data, dimensions, unknown_keyword=5)


def test_fieldset_vmin_vmax():
    data, dimensions = generate_fieldset_data(11, 11)
    fieldset = FieldSet.from_data(data, dimensions, vmin=3, vmax=7)
    assert np.isclose(np.amin(fieldset.U.data[fieldset.U.data > 0.0]), 3)
    assert np.isclose(np.amax(fieldset.U.data), 7)


@pytest.mark.parametrize("ttype", ["float", "datetime64"])
@pytest.mark.parametrize("tdim", [1, 20])
def test_fieldset_from_data_timedims(ttype, tdim):
    data, dimensions = generate_fieldset_data(10, 10, tdim=tdim)
    if ttype == "float":
        dimensions["time"] = np.linspace(0, 5, tdim)
    else:
        dimensions["time"] = [np.datetime64("2018-01-01") + np.timedelta64(t, "D") for t in range(tdim)]
    fieldset = FieldSet.from_data(data, dimensions)
    for i, dtime in enumerate(dimensions["time"]):
        assert fieldset.U.grid.time_origin.fulltime(fieldset.U.grid.time[i]) == dtime


@pytest.mark.parametrize("xdim", [100, 200])
@pytest.mark.parametrize("ydim", [100, 50])
def test_fieldset_from_data_different_dimensions(xdim, ydim):
    """Test for fieldset initialisation from data using dict-of-dict for dimensions."""
    zdim, tdim = 4, 2
    lon = np.linspace(0.0, 1.0, xdim, dtype=np.float32)
    lat = np.linspace(0.0, 1.0, ydim, dtype=np.float32)
    depth = np.zeros(zdim, dtype=np.float32)
    time = np.zeros(tdim, dtype=np.float64)
    U = np.zeros((xdim, ydim), dtype=np.float32)
    V = np.ones((xdim, ydim), dtype=np.float32)
    P = 2 * np.ones((int(xdim / 2), int(ydim / 2), zdim, tdim), dtype=np.float32)
    data = {"U": U, "V": V, "P": P}
    dimensions = {
        "U": {"lat": lat, "lon": lon},
        "V": {"lat": lat, "lon": lon},
        "P": {"lat": lat[0::2], "lon": lon[0::2], "depth": depth, "time": time},
    }

    fieldset = FieldSet.from_data(data, dimensions, transpose=True)
    assert len(fieldset.U.data.shape) == 3
    assert len(fieldset.V.data.shape) == 3
    assert len(fieldset.P.data.shape) == 4
    assert fieldset.P.data.shape == (tdim, zdim, ydim / 2, xdim / 2)
    assert np.allclose(fieldset.U.data, 0.0, rtol=1e-12)
    assert np.allclose(fieldset.V.data, 1.0, rtol=1e-12)
    assert np.allclose(fieldset.P.data, 2.0, rtol=1e-12)


@pytest.mark.parametrize("xdim", [100, 200])
@pytest.mark.parametrize("ydim", [100, 200])
def test_fieldset_from_parcels(xdim, ydim, tmpdir):
    """Simple test for fieldset initialisation from Parcels FieldSet file format."""
    filepath = tmpdir.join("test_parcels")
    data, dimensions = generate_fieldset_data(xdim, ydim)
    fieldset_out = FieldSet.from_data(data, dimensions)
    fieldset_out.write(filepath)
    fieldset = FieldSet.from_parcels(filepath)
    assert len(fieldset.U.data.shape) == 3  # Will be 4 once we use depth
    assert len(fieldset.V.data.shape) == 3
    assert np.allclose(fieldset.U.data[0, :], data["U"], rtol=1e-12)
    assert np.allclose(fieldset.V.data[0, :], data["V"], rtol=1e-12)


def test_fieldset_from_modulefile():
    nemo_fname = str(TEST_DATA / "fieldset_nemo.py")
    nemo_error_fname = str(TEST_DATA / "fieldset_nemo_error.py")

    fieldset = FieldSet.from_modulefile(nemo_fname)
    assert fieldset.U._creation_log == "from_nemo"

    indices = {"lon": range(6, 10)}
    fieldset = FieldSet.from_modulefile(nemo_fname, indices=indices)
    assert fieldset.U.grid.lon.shape[1] == 4

    with pytest.raises(IOError):
        FieldSet.from_modulefile(nemo_error_fname)

    FieldSet.from_modulefile(nemo_error_fname, modulename="random_function_name")

    with pytest.raises(IOError):
        FieldSet.from_modulefile(nemo_error_fname, modulename="none_returning_function")


def test_field_from_netcdf_fieldtypes():
    filenames = {
        "varU": {
            "lon": str(TEST_DATA / "mask_nemo_cross_180lon.nc"),
            "lat": str(TEST_DATA / "mask_nemo_cross_180lon.nc"),
            "data": str(TEST_DATA / "Uu_eastward_nemo_cross_180lon.nc"),
        },
        "varV": {
            "lon": str(TEST_DATA / "mask_nemo_cross_180lon.nc"),
            "lat": str(TEST_DATA / "mask_nemo_cross_180lon.nc"),
            "data": str(TEST_DATA / "Vv_eastward_nemo_cross_180lon.nc"),
        },
    }
    variables = {"varU": "U", "varV": "V"}
    dimensions = {"lon": "glamf", "lat": "gphif"}

    # first try without setting fieldtype
    fset = FieldSet.from_nemo(filenames, variables, dimensions)
    assert isinstance(fset.varU.units, UnitConverter)

    # now try with setting fieldtype
    fset = FieldSet.from_nemo(filenames, variables, dimensions, fieldtype={"varU": "U", "varV": "V"})
    assert isinstance(fset.varU.units, GeographicPolar)


def test_fieldset_from_agrid_dataset():
    filenames = {
        "lon": str(TEST_DATA / "mask_nemo_cross_180lon.nc"),
        "lat": str(TEST_DATA / "mask_nemo_cross_180lon.nc"),
        "data": str(TEST_DATA / "Uu_eastward_nemo_cross_180lon.nc"),
    }
    variable = {"U": "U"}
    dimensions = {"lon": "glamf", "lat": "gphif"}
    FieldSet.from_a_grid_dataset(filenames, variable, dimensions)


def test_fieldset_from_cgrid_interpmethod():
    filenames = {
        "lon": str(TEST_DATA / "mask_nemo_cross_180lon.nc"),
        "lat": str(TEST_DATA / "mask_nemo_cross_180lon.nc"),
        "data": str(TEST_DATA / "Uu_eastward_nemo_cross_180lon.nc"),
    }
    variable = "U"
    dimensions = {"lon": "glamf", "lat": "gphif"}

    with pytest.raises(TypeError):
        # should fail because FieldSet.from_c_grid_dataset does not support interp_method
        FieldSet.from_c_grid_dataset(filenames, variable, dimensions, interp_method="partialslip")


@pytest.mark.parametrize("cast_data_dtype", ["float32", "float64"])
@pytest.mark.parametrize("mode", ["scipy", "jit"])
def test_fieldset_float64(cast_data_dtype, mode, tmpdir):
    xdim, ydim = 10, 5
    lon = np.linspace(0.0, 10.0, xdim, dtype=np.float64)
    lat = np.linspace(0.0, 10.0, ydim, dtype=np.float64)
    U, V = np.meshgrid(lon, lat)
    dimensions = {"lat": lat, "lon": lon}
    data = {"U": np.array(U, dtype=np.float64), "V": np.array(V, dtype=np.float64)}

    fieldset = FieldSet.from_data(data, dimensions, mesh="flat", cast_data_dtype=cast_data_dtype)
    if cast_data_dtype == "float32":
        assert fieldset.U.data.dtype == np.float32
    else:
        assert fieldset.U.data.dtype == np.float64
    pset = ParticleSet(fieldset, ptype[mode], lon=1, lat=2)

    failed = False
    try:
        pset.execute(AdvectionRK4, runtime=2)
    except RuntimeError:
        failed = True
    if mode == "jit" and cast_data_dtype == "float64":
        assert failed
    else:
        assert np.isclose(pset[0].lon, 2.70833)
        assert np.isclose(pset[0].lat, 5.41667)
    filepath = tmpdir.join("test_fieldset_float64")
    fieldset.U.write(filepath)
    da = xr.open_dataset(str(filepath) + "U.nc")
    if cast_data_dtype == "float32":
        assert da["U"].dtype == np.float32
    else:
        assert da["U"].dtype == np.float64


@pytest.mark.parametrize("indslon", [range(10, 20), [1]])
@pytest.mark.parametrize("indslat", [range(30, 60), [22]])
def test_fieldset_from_file_subsets(indslon, indslat, tmpdir):
    """Test for subsetting fieldset from file using indices dict."""
    data, dimensions = generate_fieldset_data(100, 100)
    filepath = tmpdir.join("test_subsets")
    fieldsetfull = FieldSet.from_data(data, dimensions)
    fieldsetfull.write(filepath)
    indices = {"lon": indslon, "lat": indslat}
    indices_back = indices.copy()
    fieldsetsub = FieldSet.from_parcels(filepath, indices=indices, chunksize=None)
    assert indices == indices_back
    assert np.allclose(fieldsetsub.U.lon, fieldsetfull.U.grid.lon[indices["lon"]])
    assert np.allclose(fieldsetsub.U.lat, fieldsetfull.U.grid.lat[indices["lat"]])
    assert np.allclose(fieldsetsub.V.lon, fieldsetfull.V.grid.lon[indices["lon"]])
    assert np.allclose(fieldsetsub.V.lat, fieldsetfull.V.grid.lat[indices["lat"]])

    ixgrid = np.ix_([0], indices["lat"], indices["lon"])
    assert np.allclose(fieldsetsub.U.data, fieldsetfull.U.data[ixgrid])
    assert np.allclose(fieldsetsub.V.data, fieldsetfull.V.data[ixgrid])


def test_empty_indices(tmpdir):
    data, dimensions = generate_fieldset_data(100, 100)
    filepath = tmpdir.join("test_subsets")
    FieldSet.from_data(data, dimensions).write(filepath)
    with pytest.raises(RuntimeError):
        FieldSet.from_parcels(filepath, indices={"lon": []})


@pytest.mark.parametrize("calltype", ["from_data", "from_nemo"])
def test_illegal_dimensionsdict(calltype):
    with pytest.raises(NameError):
        if calltype == "from_data":
            data, dimensions = generate_fieldset_data(10, 10)
            dimensions["test"] = None
            FieldSet.from_data(data, dimensions)
        elif calltype == "from_nemo":
            fname = str(TEST_DATA / "mask_nemo_cross_180lon.nc")
            filenames = {"dx": fname, "mesh_mask": fname}
            variables = {"dx": "e1u"}
            dimensions = {"lon": "glamu", "lat": "gphiu", "test": "test"}
            FieldSet.from_nemo(filenames, variables, dimensions)


@pytest.mark.parametrize("xdim", [100, 200])
@pytest.mark.parametrize("ydim", [100, 200])
def test_add_field(xdim, ydim, tmpdir):
    filepath = tmpdir.join("test_add")
    data, dimensions = generate_fieldset_data(xdim, ydim)
    fieldset = FieldSet.from_data(data, dimensions)
    field = Field("newfld", fieldset.U.data, lon=fieldset.U.lon, lat=fieldset.U.lat)
    fieldset.add_field(field)
    assert fieldset.newfld.data.shape == fieldset.U.data.shape
    fieldset.write(filepath)


@pytest.mark.parametrize("dupobject", ["same", "new"])
def test_add_duplicate_field(dupobject):
    data, dimensions = generate_fieldset_data(100, 100)
    fieldset = FieldSet.from_data(data, dimensions)
    field = Field("newfld", fieldset.U.data, lon=fieldset.U.lon, lat=fieldset.U.lat)
    fieldset.add_field(field)
    with pytest.raises(RuntimeError):
        if dupobject == "same":
            fieldset.add_field(field)
        elif dupobject == "new":
            field2 = Field("newfld", np.ones((2, 2)), lon=np.array([0, 1]), lat=np.array([0, 2]))
            fieldset.add_field(field2)


@pytest.mark.parametrize("fieldtype", ["normal", "vector"])
def test_add_field_after_pset(fieldtype):
    data, dimensions = generate_fieldset_data(100, 100)
    fieldset = FieldSet.from_data(data, dimensions)
    pset = ParticleSet(fieldset, ScipyParticle, lon=0, lat=0)  # noqa ; to trigger fieldset._check_complete
    field1 = Field("field1", fieldset.U.data, lon=fieldset.U.lon, lat=fieldset.U.lat)
    field2 = Field("field2", fieldset.U.data, lon=fieldset.U.lon, lat=fieldset.U.lat)
    vfield = VectorField("vfield", field1, field2)
    with pytest.raises(RuntimeError):
        if fieldtype == "normal":
            fieldset.add_field(field1)
        elif fieldtype == "vector":
            fieldset.add_vector_field(vfield)


@pytest.mark.parametrize("chunksize", ["auto", None])
def test_fieldset_samegrids_from_file(tmpdir, chunksize):
    """Test for subsetting fieldset from file using indices dict."""
    data, dimensions = generate_fieldset_data(100, 100)
    filepath1 = tmpdir.join("test_subsets_1")
    fieldset1 = FieldSet.from_data(data, dimensions)
    fieldset1.write(filepath1)

    ufiles = [filepath1 + "U.nc"] * 4
    vfiles = [filepath1 + "V.nc"] * 4
    timestamps = np.arange(0, 4, 1) * 86400.0
    timestamps = np.expand_dims(timestamps, 1)
    files = {"U": ufiles, "V": vfiles}
    variables = {"U": "vozocrtx", "V": "vomecrty"}
    dimensions = {"lon": "nav_lon", "lat": "nav_lat"}
    fieldset = FieldSet.from_netcdf(
        files, variables, dimensions, timestamps=timestamps, allow_time_extrapolation=True, chunksize=chunksize
    )

    if chunksize == "auto":
        assert fieldset.gridset.size == 2
        assert fieldset.U.grid != fieldset.V.grid
    else:
        assert fieldset.gridset.size == 1
        assert fieldset.U.grid == fieldset.V.grid
        assert fieldset.U.chunksize == fieldset.V.chunksize


@pytest.mark.parametrize("gridtype", ["A", "C"])
def test_fieldset_dimlength1_cgrid(gridtype):
    fieldset = FieldSet.from_data({"U": 0, "V": 0}, {"lon": 0, "lat": 0})
    if gridtype == "C":
        fieldset.U.interp_method = "cgrid_velocity"
        fieldset.V.interp_method = "cgrid_velocity"
    try:
        fieldset._check_complete()
        success = True if gridtype == "A" else False
    except NotImplementedError:
        success = True if gridtype == "C" else False
    assert success


@pytest.mark.parametrize("chunksize", ["auto", None])
def test_fieldset_diffgrids_from_file(tmpdir, chunksize):
    """Test for subsetting fieldset from file using indices dict."""
    filename = "test_subsets"
    data, dimensions = generate_fieldset_data(100, 100)
    filepath1 = tmpdir.join(filename + "_1")
    fieldset1 = FieldSet.from_data(data, dimensions)
    fieldset1.write(filepath1)
    data, dimensions = generate_fieldset_data(50, 50)
    filepath2 = tmpdir.join(filename + "_2")
    fieldset2 = FieldSet.from_data(data, dimensions)
    fieldset2.write(filepath2)

    ufiles = [filepath1 + "U.nc"] * 4
    vfiles = [filepath2 + "V.nc"] * 4
    timestamps = np.arange(0, 4, 1) * 86400.0
    timestamps = np.expand_dims(timestamps, 1)
    files = {"U": ufiles, "V": vfiles}
    variables = {"U": "vozocrtx", "V": "vomecrty"}
    dimensions = {"lon": "nav_lon", "lat": "nav_lat"}

    fieldset = FieldSet.from_netcdf(
        files, variables, dimensions, timestamps=timestamps, allow_time_extrapolation=True, chunksize=chunksize
    )
    assert fieldset.gridset.size == 2
    assert fieldset.U.grid != fieldset.V.grid


@pytest.mark.parametrize("chunksize", ["auto", None])
def test_fieldset_diffgrids_from_file_data(tmpdir, chunksize):
    """Test for subsetting fieldset from file using indices dict."""
    data, dimensions = generate_fieldset_data(100, 100)
    filepath = tmpdir.join("test_subsets")
    fieldset_data = FieldSet.from_data(data, dimensions)
    fieldset_data.write(filepath)
    field_data = fieldset_data.U
    field_data.name = "B"

    ufiles = [filepath + "U.nc"] * 4
    vfiles = [filepath + "V.nc"] * 4
    timestamps = np.arange(0, 4, 1) * 86400.0
    timestamps = np.expand_dims(timestamps, 1)
    files = {"U": ufiles, "V": vfiles}
    variables = {"U": "vozocrtx", "V": "vomecrty"}
    dimensions = {"lon": "nav_lon", "lat": "nav_lat"}
    fieldset_file = FieldSet.from_netcdf(
        files, variables, dimensions, timestamps=timestamps, allow_time_extrapolation=True, chunksize=chunksize
    )

    fieldset_file.add_field(field_data, "B")
    fields = [f for f in fieldset_file.get_fields() if isinstance(f, Field)]
    assert len(fields) == 3
    if chunksize == "auto":
        assert fieldset_file.gridset.size == 3
    else:
        assert fieldset_file.gridset.size == 2
    assert fieldset_file.U.grid != fieldset_file.B.grid


def test_fieldset_samegrids_from_data():
    """Test for subsetting fieldset from file using indices dict."""
    data, dimensions = generate_fieldset_data(100, 100)
    fieldset1 = FieldSet.from_data(data, dimensions)
    field_data = fieldset1.U
    field_data.name = "B"
    fieldset1.add_field(field_data, "B")
    assert fieldset1.gridset.size == 1
    assert fieldset1.U.grid == fieldset1.B.grid


@pytest.mark.parametrize("dx, dy", [("e1u", "e2u"), ("e1v", "e2v")])
def test_fieldset_celledgesizes_curvilinear(dx, dy):
    fname = str(TEST_DATA / "mask_nemo_cross_180lon.nc")
    filenames = {"dx": fname, "dy": fname, "mesh_mask": fname}
    variables = {"dx": dx, "dy": dy}
    dimensions = {"dx": {"lon": "glamu", "lat": "gphiu"}, "dy": {"lon": "glamu", "lat": "gphiu"}}
    fieldset = FieldSet.from_nemo(filenames, variables, dimensions)

    # explicitly setting cell_edge_sizes from e1u and e2u etc
    fieldset.dx.grid.cell_edge_sizes["x"] = fieldset.dx.data
    fieldset.dx.grid.cell_edge_sizes["y"] = fieldset.dy.data

    A = fieldset.dx.cell_areas()
    assert np.allclose(A, fieldset.dx.data * fieldset.dy.data)


def test_fieldset_write_curvilinear(tmpdir):
    fname = str(TEST_DATA / "mask_nemo_cross_180lon.nc")
    filenames = {"dx": fname, "mesh_mask": fname}
    variables = {"dx": "e1u"}
    dimensions = {"lon": "glamu", "lat": "gphiu"}
    fieldset = FieldSet.from_nemo(filenames, variables, dimensions)
    assert fieldset.dx._creation_log == "from_nemo"

    newfile = tmpdir.join("curv_field")
    fieldset.write(newfile)

    fieldset2 = FieldSet.from_netcdf(
        filenames=newfile + "dx.nc",
        variables={"dx": "dx"},
        dimensions={"time": "time_counter", "depth": "depthdx", "lon": "nav_lon", "lat": "nav_lat"},
    )
    assert fieldset2.dx._creation_log == "from_netcdf"

    for var in ["lon", "lat", "data"]:
        assert np.allclose(getattr(fieldset2.dx, var), getattr(fieldset.dx, var))


def test_curv_fieldset_add_periodic_halo():
    fname = str(TEST_DATA / "mask_nemo_cross_180lon.nc")
    filenames = {"dx": fname, "dy": fname, "mesh_mask": fname}
    variables = {"dx": "e1u", "dy": "e1v"}
    dimensions = {"dx": {"lon": "glamu", "lat": "gphiu"}, "dy": {"lon": "glamu", "lat": "gphiu"}}
    fieldset = FieldSet.from_nemo(filenames, variables, dimensions)

    with pytest.raises(NotImplementedError):
        fieldset.add_periodic_halo(zonal=3, meridional=2)


@pytest.mark.parametrize("mesh", ["flat", "spherical"])
def test_fieldset_cellareas(mesh):
    data, dimensions = generate_fieldset_data(10, 7)
    fieldset = FieldSet.from_data(data, dimensions, mesh=mesh)
    cell_areas = fieldset.V.cell_areas()
    if mesh == "flat":
        assert np.allclose(cell_areas.flatten(), cell_areas[0, 0], rtol=1e-3)
    else:
        assert all(
            (np.gradient(cell_areas, axis=0) < 0).flatten()
        )  # areas should decrease with latitude in spherical mesh
        for y in range(cell_areas.shape[0]):
            assert np.allclose(cell_areas[y, :], cell_areas[y, 0], rtol=1e-3)


def addConst(particle, fieldset, time):  # pragma: no cover
    particle.lon = particle.lon + fieldset.movewest + fieldset.moveeast


@pytest.mark.parametrize("mode", ["scipy", "jit"])
def test_fieldset_constant(mode):
    data, dimensions = generate_fieldset_data(100, 100)
    fieldset = FieldSet.from_data(data, dimensions)
    westval = -0.2
    eastval = 0.3
    fieldset.add_constant("movewest", westval)
    fieldset.add_constant("moveeast", eastval)
    assert fieldset.movewest == westval

    pset = ParticleSet.from_line(fieldset, size=1, pclass=ptype[mode], start=(0.5, 0.5), finish=(0.5, 0.5))
    pset.execute(pset.Kernel(addConst), dt=1, runtime=1)
    assert abs(pset.lon[0] - (0.5 + westval + eastval)) < 1e-4


@pytest.mark.parametrize("mode", ["scipy", "jit"])
@pytest.mark.parametrize("swapUV", [False, True])
def test_vector_fields(mode, swapUV):
    lon = np.linspace(0.0, 10.0, 12, dtype=np.float32)
    lat = np.linspace(0.0, 10.0, 10, dtype=np.float32)
    U = np.ones((10, 12), dtype=np.float32)
    V = np.zeros((10, 12), dtype=np.float32)
    data = {"U": U, "V": V}
    dimensions = {"U": {"lat": lat, "lon": lon}, "V": {"lat": lat, "lon": lon}}
    fieldset = FieldSet.from_data(data, dimensions, mesh="flat")
    if swapUV:  # we test that we can freely edit whatever UV field
        UV = VectorField("UV", fieldset.V, fieldset.U)
        fieldset.add_vector_field(UV)

    pset = ParticleSet.from_line(fieldset, size=1, pclass=ptype[mode], start=(0.5, 0.5), finish=(0.5, 0.5))
    pset.execute(AdvectionRK4, dt=1, runtime=2)
    if swapUV:
        assert abs(pset.lon[0] - 0.5) < 1e-9
        assert abs(pset.lat[0] - 1.5) < 1e-9
    else:
        assert abs(pset.lon[0] - 1.5) < 1e-9
        assert abs(pset.lat[0] - 0.5) < 1e-9


@pytest.mark.parametrize("mode", ["scipy", "jit"])
def test_add_second_vector_field(mode):
    lon = np.linspace(0.0, 10.0, 12, dtype=np.float32)
    lat = np.linspace(0.0, 10.0, 10, dtype=np.float32)
    U = np.ones((10, 12), dtype=np.float32)
    V = np.zeros((10, 12), dtype=np.float32)
    data = {"U": U, "V": V}
    dimensions = {"U": {"lat": lat, "lon": lon}, "V": {"lat": lat, "lon": lon}}
    fieldset = FieldSet.from_data(data, dimensions, mesh="flat")

    data2 = {"U2": U, "V2": V}
    dimensions2 = {"lon": [ln + 0.1 for ln in lon], "lat": [lt - 0.1 for lt in lat]}
    fieldset2 = FieldSet.from_data(data2, dimensions2, mesh="flat")

    UV2 = VectorField("UV2", fieldset2.U2, fieldset2.V2)
    fieldset.add_vector_field(UV2)

    def SampleUV2(particle, fieldset, time):  # pragma: no cover
        u, v = fieldset.UV2[time, particle.depth, particle.lat, particle.lon]
        particle_dlon += u * particle.dt  # noqa
        particle_dlat += v * particle.dt  # noqa

    pset = ParticleSet(fieldset, pclass=ptype[mode], lon=0.5, lat=0.5)
    pset.execute(AdvectionRK4 + pset.Kernel(SampleUV2), dt=1, runtime=2)

    assert abs(pset.lon[0] - 2.5) < 1e-9
    assert abs(pset.lat[0] - 0.5) < 1e-9


def test_fieldset_write(tmp_zarrfile):
    xdim, ydim = 3, 4
    lon = np.linspace(0.0, 10.0, xdim, dtype=np.float32)
    lat = np.linspace(0.0, 10.0, ydim, dtype=np.float32)
    U = np.ones((ydim, xdim), dtype=np.float32)
    V = np.zeros((ydim, xdim), dtype=np.float32)
    data = {"U": U, "V": V}
    dimensions = {"U": {"lat": lat, "lon": lon}, "V": {"lat": lat, "lon": lon}}
    fieldset = FieldSet.from_data(data, dimensions, mesh="flat")

    fieldset.U.to_write = True

    def UpdateU(particle, fieldset, time):  # pragma: no cover
        tmp1, tmp2 = fieldset.UV[particle]
        fieldset.U.data[particle.ti, particle.yi, particle.xi] += 1
        fieldset.U.grid.time[0] = time

    pset = ParticleSet(fieldset, pclass=ScipyParticle, lon=5, lat=5)
    ofile = pset.ParticleFile(name=tmp_zarrfile, outputdt=2.0)
    pset.execute(UpdateU, dt=1, runtime=10, output_file=ofile)

    assert fieldset.U.data[0, 1, 0] == 11

    da = xr.open_dataset(str(tmp_zarrfile).replace(".zarr", "_0005U.nc"))
    assert np.allclose(fieldset.U.data, da["U"].values, atol=1.0)


@pytest.mark.flaky
@pytest.mark.parametrize("mode", ["scipy", "jit"])
@pytest.mark.parametrize("time_periodic", [4 * 86400.0, False])
@pytest.mark.parametrize("dt", [-3600, 3600])
@pytest.mark.parametrize(
    "chunksize", [False, "auto", {"time": ("time_counter", 1), "lat": ("y", 32), "lon": ("x", 32)}]
)
@pytest.mark.parametrize("with_GC", [False, True])
@pytest.mark.skipif(sys.platform.startswith("win"), reason="skipping windows test as windows memory leaks (#787)")
def test_from_netcdf_memory_containment(mode, time_periodic, dt, chunksize, with_GC):
    if time_periodic and dt < 0:
        return  # time_periodic does not work in backward-time mode
    if chunksize == "auto":
        dask.config.set({"array.chunk-size": "2MiB"})
    else:
        dask.config.set({"array.chunk-size": "128MiB"})

    class PerformanceLog:
        samples = []
        memory_steps = []
        _iter = 0

        def advance(self):
            process = psutil.Process(os.getpid())
            self.memory_steps.append(process.memory_info().rss)
            self.samples.append(self._iter)
            self._iter += 1

    def perIterGC():
        gc.collect()

    def periodicBoundaryConditions(particle, fieldset, time):  # pragma: no cover
        while particle.lon > 180.0:
            particle_dlon -= 360.0  # noqa
        while particle.lon < -180.0:
            particle_dlon += 360.0
        while particle.lat > 90.0:
            particle_dlat -= 180.0  # noqa
        while particle.lat < -90.0:
            particle_dlat += 180.0

    process = psutil.Process(os.getpid())
    mem_0 = process.memory_info().rss
    fnameU = str(TEST_DATA / "perlinfieldsU.nc")
    fnameV = str(TEST_DATA / "perlinfieldsV.nc")
    ufiles = [fnameU] * 4
    vfiles = [fnameV] * 4
    timestamps = np.arange(0, 4, 1) * 86400.0
    timestamps = np.expand_dims(timestamps, 1)
    files = {"U": ufiles, "V": vfiles}
    variables = {"U": "vozocrtx", "V": "vomecrty"}
    dimensions = {"lon": "nav_lon", "lat": "nav_lat"}

    fieldset = FieldSet.from_netcdf(
        files,
        variables,
        dimensions,
        timestamps=timestamps,
        time_periodic=time_periodic,
        allow_time_extrapolation=True if time_periodic in [False, None] else False,
        chunksize=chunksize,
    )
    perflog = PerformanceLog()
    postProcessFuncs = [perflog.advance]
    if with_GC:
        postProcessFuncs.append(perIterGC)
    pset = ParticleSet(fieldset=fieldset, pclass=ptype[mode], lon=[0.5], lat=[0.5])
    mem_0 = process.memory_info().rss
    mem_exhausted = False
    try:
        pset.execute(
            pset.Kernel(AdvectionRK4) + periodicBoundaryConditions,
            dt=dt,
            runtime=timedelta(days=7),
            postIterationCallbacks=postProcessFuncs,
            callbackdt=timedelta(hours=12),
        )
    except MemoryError:
        mem_exhausted = True
    mem_steps_np = np.array(perflog.memory_steps)
    if with_GC:
        assert np.allclose(mem_steps_np[8:], perflog.memory_steps[-1], rtol=0.01)
    if (chunksize is not False or with_GC) and mode != "scipy":
        assert np.all((mem_steps_np - mem_0) <= 5275648)  # represents 4 x [U|V] * sizeof(field data) + 562816
    assert not mem_exhausted


@pytest.mark.parametrize("mode", ["scipy", "jit"])
@pytest.mark.parametrize("time_periodic", [4 * 86400.0, False])
@pytest.mark.parametrize(
    "chunksize",
    [
        False,
        "auto",
        {"lat": ("y", 32), "lon": ("x", 32)},
        {"time": ("time_counter", 1), "lat": ("y", 32), "lon": ("x", 32)},
    ],
)
@pytest.mark.parametrize("deferLoad", [True, False])
def test_from_netcdf_chunking(mode, time_periodic, chunksize, deferLoad):
    fnameU = str(TEST_DATA / "perlinfieldsU.nc")
    fnameV = str(TEST_DATA / "perlinfieldsV.nc")
    ufiles = [fnameU] * 4
    vfiles = [fnameV] * 4
    timestamps = np.arange(0, 4, 1) * 86400.0
    timestamps = np.expand_dims(timestamps, 1)
    files = {"U": ufiles, "V": vfiles}
    variables = {"U": "vozocrtx", "V": "vomecrty"}
    dimensions = {"lon": "nav_lon", "lat": "nav_lat"}

    fieldset = FieldSet.from_netcdf(
        files,
        variables,
        dimensions,
        timestamps=timestamps,
        time_periodic=time_periodic,
        deferred_load=deferLoad,
        allow_time_extrapolation=True if time_periodic in [False, None] else False,
        chunksize=chunksize,
    )
    pset = ParticleSet.from_line(fieldset, size=1, pclass=ptype[mode], start=(0.5, 0.5), finish=(0.5, 0.5))
    pset.execute(AdvectionRK4, dt=1, runtime=1)


@pytest.mark.parametrize("datetype", ["float", "datetime64"])
def test_timestamps(datetype, tmpdir):
    data1, dims1 = generate_fieldset_data(10, 10, 1, 10)
    data2, dims2 = generate_fieldset_data(10, 10, 1, 4)
    if datetype == "float":
        dims1["time"] = np.arange(0, 10, 1) * 86400
        dims2["time"] = np.arange(10, 14, 1) * 86400
    else:
        dims1["time"] = np.arange("2005-02-01", "2005-02-11", dtype="datetime64[D]")
        dims2["time"] = np.arange("2005-02-11", "2005-02-15", dtype="datetime64[D]")

    fieldset1 = FieldSet.from_data(data1, dims1)
    fieldset1.U.data[0, :, :] = 2.0
    fieldset1.write(tmpdir.join("file1"))

    fieldset2 = FieldSet.from_data(data2, dims2)
    fieldset2.U.data[0, :, :] = 0.0
    fieldset2.write(tmpdir.join("file2"))

    fieldset3 = FieldSet.from_parcels(tmpdir.join("file*"), time_periodic=timedelta(days=14))
    timestamps = [dims1["time"], dims2["time"]]
    fieldset4 = FieldSet.from_parcels(tmpdir.join("file*"), timestamps=timestamps, time_periodic=timedelta(days=14))
    assert np.allclose(fieldset3.U.grid.time_full, fieldset4.U.grid.time_full)

    for d in [0, 8, 10, 13]:
        fieldset3.computeTimeChunk(d * 86400.0, 1.0)
        fieldset4.computeTimeChunk(d * 86400.0, 1.0)
        assert np.allclose(fieldset3.U.data, fieldset4.U.data)


@pytest.mark.parametrize("mode", ["scipy", "jit"])
@pytest.mark.parametrize("use_xarray", [True, False])
@pytest.mark.parametrize("time_periodic", [86400.0, False])
@pytest.mark.parametrize("dt_sign", [-1, 1])
def test_periodic(mode, use_xarray, time_periodic, dt_sign):
    lon = np.array([0, 1], dtype=np.float32)
    lat = np.array([0, 1], dtype=np.float32)
    depth = np.array([0, 1], dtype=np.float32)
    tsize = 24 * 60 + 1
    period = 86400
    time = np.linspace(0, period, tsize, dtype=np.float64)

    def temp_func(time):
        return 20 + 2 * np.sin(time * 2 * np.pi / period)

    temp_vec = temp_func(time)

    U = np.zeros((2, 2, 2, tsize), dtype=np.float32)
    V = np.zeros((2, 2, 2, tsize), dtype=np.float32)
    V[0, 0, 0, :] = 1e-5
    W = np.zeros((2, 2, 2, tsize), dtype=np.float32)
    temp = np.zeros((2, 2, 2, tsize), dtype=np.float32)
    temp[:, :, :, :] = temp_vec
    D = np.ones((2, 2), dtype=np.float32)  # adding non-timevarying field

    full_dims = {"lon": lon, "lat": lat, "depth": depth, "time": time}
    dimensions = {"U": full_dims, "V": full_dims, "W": full_dims, "temp": full_dims, "D": {"lon": lon, "lat": lat}}
    if use_xarray:
        coords = {"lat": lat, "lon": lon, "depth": depth, "time": time}
        variables = {"U": "Uxr", "V": "Vxr", "W": "Wxr", "temp": "Txr", "D": "Dxr"}
        dimnames = {"lon": "lon", "lat": "lat", "depth": "depth", "time": "time"}
        ds = xr.Dataset(
            {
                "Uxr": xr.DataArray(U, coords=coords, dims=("lon", "lat", "depth", "time")),
                "Vxr": xr.DataArray(V, coords=coords, dims=("lon", "lat", "depth", "time")),
                "Wxr": xr.DataArray(W, coords=coords, dims=("lon", "lat", "depth", "time")),
                "Txr": xr.DataArray(temp, coords=coords, dims=("lon", "lat", "depth", "time")),
                "Dxr": xr.DataArray(D, coords={"lat": lat, "lon": lon}, dims=("lon", "lat")),
            }
        )
        fieldset = FieldSet.from_xarray_dataset(
            ds,
            variables,
            {"U": dimnames, "V": dimnames, "W": dimnames, "temp": dimnames, "D": {"lon": "lon", "lat": "lat"}},
            time_periodic=time_periodic,
            transpose=True,
            allow_time_extrapolation=True,
        )
    else:
        data = {"U": U, "V": V, "W": W, "temp": temp, "D": D}
        fieldset = FieldSet.from_data(
            data, dimensions, mesh="flat", time_periodic=time_periodic, transpose=True, allow_time_extrapolation=True
        )

    def sampleTemp(particle, fieldset, time):  # pragma: no cover
        particle.temp = fieldset.temp[time, particle.depth, particle.lat, particle.lon]
        # test if we can interpolate UV and UVW together
        (particle.u1, particle.v1) = fieldset.UV[time, particle.depth, particle.lat, particle.lon]
        (particle.u2, particle.v2, w_) = fieldset.UVW[time, particle.depth, particle.lat, particle.lon]
        # test if we can sample a non-timevarying field too
        particle.d = fieldset.D[0, 0, particle.lat, particle.lon]

    MyParticle = ptype[mode].add_variables(
        [
            Variable("temp", dtype=np.float32, initial=20.0),
            Variable("u1", dtype=np.float32, initial=0.0),
            Variable("u2", dtype=np.float32, initial=0.0),
            Variable("v1", dtype=np.float32, initial=0.0),
            Variable("v2", dtype=np.float32, initial=0.0),
            Variable("d", dtype=np.float32, initial=0.0),
        ]
    )

    pset = ParticleSet.from_list(fieldset, pclass=MyParticle, lon=[0.5], lat=[0.5], depth=[0.5])
    pset.execute(
        AdvectionRK4_3D + pset.Kernel(sampleTemp), runtime=timedelta(hours=51), dt=timedelta(hours=dt_sign * 1)
    )

    if time_periodic is not False:
        t = pset.time[0]
        temp_theo = temp_func(t)
    elif dt_sign == 1:
        temp_theo = temp_vec[-1]
    elif dt_sign == -1:
        temp_theo = temp_vec[0]
    assert np.allclose(temp_theo, pset.temp[0], atol=1e-5)
    assert np.allclose(pset.u1[0], pset.u2[0])
    assert np.allclose(pset.v1[0], pset.v2[0])
    assert np.allclose(pset.d[0], 1.0)


@pytest.mark.parametrize("fail", [False, pytest.param(True, marks=pytest.mark.xfail(strict=True))])
def test_fieldset_defer_loading_with_diff_time_origin(tmpdir, fail):
    filepath = tmpdir.join("test_parcels_defer_loading")
    data0, dims0 = generate_fieldset_data(10, 10, 1, 10)
    dims0["time"] = np.arange(0, 10, 1) * 3600
    fieldset_out = FieldSet.from_data(data0, dims0)
    fieldset_out.U.grid._time_origin = TimeConverter(np.datetime64("2018-04-20"))
    fieldset_out.V.grid._time_origin = TimeConverter(np.datetime64("2018-04-20"))
    data1, dims1 = generate_fieldset_data(10, 10, 1, 10)
    if fail:
        dims1["time"] = np.arange(0, 10, 1) * 3600
    else:
        dims1["time"] = np.arange(0, 10, 1) * 1800 + (24 + 25) * 3600
    if fail:
        Wtime_origin = TimeConverter(np.datetime64("2018-04-22"))
    else:
        Wtime_origin = TimeConverter(np.datetime64("2018-04-18"))
    gridW = RectilinearZGrid(dims1["lon"], dims1["lat"], dims1["depth"], dims1["time"], time_origin=Wtime_origin)
    fieldW = Field("W", np.zeros(data1["U"].shape), grid=gridW)
    fieldset_out.add_field(fieldW)
    fieldset_out.write(filepath)
    fieldset = FieldSet.from_parcels(filepath, extra_fields={"W": "W"})
    assert fieldset.U._creation_log == "from_parcels"
    pset = ParticleSet.from_list(
        fieldset, pclass=JITParticle, lon=[0.5], lat=[0.5], depth=[0.5], time=[datetime.datetime(2018, 4, 20, 1)]
    )
    pset.execute(AdvectionRK4_3D, runtime=timedelta(hours=4), dt=timedelta(hours=1))


@pytest.mark.parametrize("zdim", [2, 8])
@pytest.mark.parametrize("scale_fac", [0.2, 4, 1])
def test_fieldset_defer_loading_function(zdim, scale_fac, tmpdir):
    filepath = tmpdir.join("test_parcels_defer_loading")
    data0, dims0 = generate_fieldset_data(3, 3, zdim, 10)
    data0["U"][:, 0, :, :] = (
        np.nan
    )  # setting first layer to nan, which will be changed to zero (and all other layers to 1)
    dims0["time"] = np.arange(0, 10, 1) * 3600
    dims0["depth"] = np.arange(0, zdim, 1)
    fieldset_out = FieldSet.from_data(data0, dims0)
    fieldset_out.write(filepath)
    fieldset = FieldSet.from_parcels(
        filepath, chunksize={"time": ("time_counter", 1), "depth": ("depthu", 1), "lat": ("y", 2), "lon": ("x", 2)}
    )

    # testing for combination of deferred-loaded and numpy Fields
    with pytest.raises(ValueError):
        fieldset.add_field(Field("numpyfield", np.zeros((10, zdim, 3, 3)), grid=fieldset.U.grid))

    # testing for scaling factors
    fieldset.U.set_scaling_factor(scale_fac)

    dz = np.gradient(fieldset.U.depth)
    DZ = np.moveaxis(np.tile(dz, (fieldset.U.grid.ydim, fieldset.U.grid.xdim, 1)), [0, 1, 2], [1, 2, 0])

    def compute(fieldset):
        # Calculating vertical weighted average
        f: Field
        for f in [fieldset.U, fieldset.V]:
            for tind in f._loaded_time_indices:
                data = da.sum(f.data[tind, :] * DZ, axis=0) / sum(dz)
                data = da.broadcast_to(data, (1, f.grid.zdim, f.grid.ydim, f.grid.xdim))
                f.data = f._data_concatenate(f.data, data, tind)

    fieldset.compute_on_defer = compute
    fieldset.computeTimeChunk(1, 1)
    assert isinstance(fieldset.U.data, da.core.Array)
    assert np.allclose(fieldset.U.data, scale_fac * (zdim - 1.0) / zdim)

    pset = ParticleSet(fieldset, JITParticle, 0, 0)

    pset.execute(DoNothing, dt=3600)
    assert np.allclose(fieldset.U.data, scale_fac * (zdim - 1.0) / zdim)


@pytest.mark.parametrize("time2", [1, 7])
def test_fieldset_initialisation_kernel_dask(time2, tmpdir):
    filepath = tmpdir.join("test_parcels_defer_loading")
    data0, dims0 = generate_fieldset_data(3, 3, 4, 10)
    data0["U"] = np.random.rand(10, 4, 3, 3)
    dims0["time"] = np.arange(0, 10, 1)
    dims0["depth"] = np.arange(0, 4, 1)
    fieldset_out = FieldSet.from_data(data0, dims0)
    fieldset_out.write(filepath)
    fieldset = FieldSet.from_parcels(
        filepath, chunksize={"time": ("time_counter", 1), "depth": ("depthu", 1), "lat": ("y", 2), "lon": ("x", 2)}
    )

    def SampleField(particle, fieldset, time):  # pragma: no cover
        particle.u_kernel, particle.v_kernel = fieldset.UV[time, particle.depth, particle.lat, particle.lon]

    SampleParticle = JITParticle.add_variables(
        [
            Variable("u_kernel", dtype=np.float32, initial=0.0),
            Variable("v_kernel", dtype=np.float32, initial=0.0),
            Variable("u_scipy", dtype=np.float32, initial=0.0),
        ]
    )

    pset = ParticleSet(
        fieldset, pclass=SampleParticle, time=[0, time2], lon=[0.5, 0.5], lat=[0.5, 0.5], depth=[0.5, 0.5]
    )

    if time2 > 1:
        with pytest.raises(TimeExtrapolationError):
            pset.execute(SampleField, runtime=10)
    else:
        pset.execute(SampleField, runtime=1)
        assert np.allclose([p.u_kernel for p in pset], [p.u_scipy for p in pset], atol=1e-5)
        assert isinstance(fieldset.U.data, da.core.Array)


@pytest.mark.parametrize("tdim", [10, None])
def test_fieldset_from_xarray(tdim):
    def generate_dataset(xdim, ydim, zdim=1, tdim=1):
        lon = np.linspace(0.0, 12, xdim, dtype=np.float32)
        lat = np.linspace(0.0, 12, ydim, dtype=np.float32)
        depth = np.linspace(0.0, 20.0, zdim, dtype=np.float32)
        if tdim:
            time = np.linspace(0.0, 10, tdim, dtype=np.float64)
            Uxr = np.ones((tdim, zdim, ydim, xdim), dtype=np.float32)
            Vxr = np.ones((tdim, zdim, ydim, xdim), dtype=np.float32)
            for t in range(Uxr.shape[0]):
                Uxr[t, :, :, :] = t / 10.0
            coords = {"lat": lat, "lon": lon, "depth": depth, "time": time}
            dims = ("time", "depth", "lat", "lon")
        else:
            Uxr = np.ones((zdim, ydim, xdim), dtype=np.float32)
            Vxr = np.ones((zdim, ydim, xdim), dtype=np.float32)
            for z in range(Uxr.shape[0]):
                Uxr[z, :, :] = z / 2.0
            coords = {"lat": lat, "lon": lon, "depth": depth}
            dims = ("depth", "lat", "lon")
        return xr.Dataset(
            {"Uxr": xr.DataArray(Uxr, coords=coords, dims=dims), "Vxr": xr.DataArray(Vxr, coords=coords, dims=dims)}
        )

    ds = generate_dataset(3, 3, 2, tdim)
    variables = {"U": "Uxr", "V": "Vxr"}
    if tdim:
        dimensions = {"lat": "lat", "lon": "lon", "depth": "depth", "time": "time"}
    else:
        dimensions = {"lat": "lat", "lon": "lon", "depth": "depth"}
    fieldset = FieldSet.from_xarray_dataset(ds, variables, dimensions, mesh="flat")
    assert fieldset.U._creation_log == "from_xarray_dataset"

    pset = ParticleSet(fieldset, JITParticle, 0, 0, depth=20)

    pset.execute(AdvectionRK4, dt=1, runtime=10)
    if tdim == 10:
        assert np.allclose(pset.lon_nextloop[0], 4.5) and np.allclose(pset.lat_nextloop[0], 10)
    else:
        assert np.allclose(pset.lon_nextloop[0], 5.0) and np.allclose(pset.lat_nextloop[0], 10)


@pytest.mark.parametrize("mode", ["scipy", "jit"])
def test_fieldset_frompop(mode):
    filenames = str(TEST_DATA / "POPtestdata_time.nc")
    variables = {"U": "U", "V": "V", "W": "W", "T": "T"}
    dimensions = {"lon": "lon", "lat": "lat", "time": "time"}

    fieldset = FieldSet.from_pop(filenames, variables, dimensions, mesh="flat")
    pset = ParticleSet.from_list(fieldset, ptype[mode], lon=[3, 5, 1], lat=[3, 5, 1])
    pset.execute(AdvectionRK4, runtime=3, dt=1)


def test_fieldset_from_data_gridtypes():
    """Simple test for fieldset initialisation from data."""
    xdim, ydim, zdim = 20, 10, 4

    lon = np.linspace(0.0, 10.0, xdim, dtype=np.float32)
    lat = np.linspace(0.0, 10.0, ydim, dtype=np.float32)
    depth = np.linspace(0.0, 1.0, zdim, dtype=np.float32)
    depth_s = np.ones((zdim, ydim, xdim))
    U = np.ones((zdim, ydim, xdim))
    V = np.ones((zdim, ydim, xdim))
    dimensions = {"lat": lat, "lon": lon, "depth": depth}
    data = {"U": np.array(U, dtype=np.float32), "V": np.array(V, dtype=np.float32)}
    lonm, latm = np.meshgrid(lon, lat)
    for k in range(zdim):
        data["U"][k, :, :] = lonm * (depth[k] + 1) + 0.1
        depth_s[k, :, :] = depth[k]

    # Rectilinear Z grid
    fieldset = FieldSet.from_data(data, dimensions, mesh="flat")
    pset = ParticleSet(fieldset, ScipyParticle, [0, 0], [0, 0], [0, 0.4])
    pset.execute(AdvectionRK4, runtime=1.5, dt=0.5)
    plon = pset.lon
    plat = pset.lat
    # sol of  dx/dt = (init_depth+1)*x+0.1; x(0)=0
    assert np.allclose(plon, [0.17173462592827032, 0.2177736932123214])
    assert np.allclose(plat, [1, 1])

    # Rectilinear S grid
    dimensions["depth"] = depth_s
    fieldset = FieldSet.from_data(data, dimensions, mesh="flat")
    pset = ParticleSet(fieldset, ScipyParticle, [0, 0], [0, 0], [0, 0.4])
    pset.execute(AdvectionRK4, runtime=1.5, dt=0.5)
    assert np.allclose(plon, pset.lon)
    assert np.allclose(plat, pset.lat)

    # Curvilinear Z grid
    dimensions["lon"] = lonm
    dimensions["lat"] = latm
    dimensions["depth"] = depth
    fieldset = FieldSet.from_data(data, dimensions, mesh="flat")
    pset = ParticleSet(fieldset, ScipyParticle, [0, 0], [0, 0], [0, 0.4])
    pset.execute(AdvectionRK4, runtime=1.5, dt=0.5)
    assert np.allclose(plon, pset.lon)
    assert np.allclose(plat, pset.lat)

    # Curvilinear S grid
    dimensions["depth"] = depth_s
    fieldset = FieldSet.from_data(data, dimensions, mesh="flat")
    pset = ParticleSet(fieldset, ScipyParticle, [0, 0], [0, 0], [0, 0.4])
    pset.execute(AdvectionRK4, runtime=1.5, dt=0.5)
    assert np.allclose(plon, pset.lon)
    assert np.allclose(plat, pset.lat)


@pytest.mark.parametrize("mode", ["scipy", "jit"])
@pytest.mark.parametrize("direction", [1, -1])
@pytest.mark.parametrize("time_extrapolation", [True, False])
def test_deferredload_simplefield(mode, direction, time_extrapolation, tmpdir):
    tdim = 10
    filename = tmpdir.join("simplefield_deferredload.nc")
    data = np.zeros((tdim, 2, 2))
    for ti in range(tdim):
        data[ti, :, :] = ti if direction == 1 else tdim - ti - 1
    ds = xr.Dataset(
        {"U": (("t", "y", "x"), data), "V": (("t", "y", "x"), data)},
        coords={"x": [0, 1], "y": [0, 1], "t": np.arange(tdim)},
    )
    ds.to_netcdf(filename)

    fieldset = FieldSet.from_netcdf(
        filename,
        {"U": "U", "V": "V"},
        {"lon": "x", "lat": "y", "time": "t"},
        deferred_load=True,
        mesh="flat",
        allow_time_extrapolation=time_extrapolation,
    )

    SamplingParticle = ptype[mode].add_variable("p")
    pset = ParticleSet(fieldset, SamplingParticle, lon=0.5, lat=0.5)

    def SampleU(particle, fieldset, time):  # pragma: no cover
        particle.p, tmp = fieldset.UV[particle]

    runtime = tdim * 2 if time_extrapolation else None
    pset.execute(SampleU, dt=direction, runtime=runtime)
    assert pset.p == tdim - 1 if time_extrapolation else tdim - 2


def test_daskfieldfilebuffer_dimnames():
    DaskFileBuffer.add_to_dimension_name_map_global({"lat": "nydim", "lon": "nxdim"})
    fnameU = str(TEST_DATA / "perlinfieldsU.nc")
    dimensions = {"lon": "nav_lon", "lat": "nav_lat"}
    fb = DaskFileBuffer(fnameU, dimensions, indices={})
    assert ("nxdim" in fb._static_name_maps["lon"]) and ("ntdim" not in fb._static_name_maps["time"])
    fb.add_to_dimension_name_map({"time": "ntdim", "depth": "nddim"})
    assert ("nxdim" in fb._static_name_maps["lon"]) and ("ntdim" in fb._static_name_maps["time"])
    assert fb._get_available_dims_indices_by_request() == {"time": None, "depth": None, "lat": 0, "lon": 1}
    assert fb._get_available_dims_indices_by_namemap() == {"time": 0, "depth": 1, "lat": 2, "lon": 3}
    assert fb._is_dimension_chunked("lon") is False
    assert fb._is_dimension_in_chunksize_request("lon") == (-1, "", 0)
