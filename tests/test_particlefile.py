import os
import tempfile
from datetime import timedelta

import cftime
import numpy as np
import pytest
import xarray as xr
from zarr.storage import MemoryStore

import parcels
from parcels import (
    AdvectionRK4,
    Field,
    FieldSet,
    JITParticle,
    ParticleSet,
    ScipyParticle,
    Variable,
)
from parcels.particlefile import _set_calendar
from parcels.tools.converters import _get_cftime_calendars, _get_cftime_datetimes
from tests.common_kernels import DoNothing
from tests.utils import create_fieldset_zeros_simple

ptype = {"scipy": ScipyParticle, "jit": JITParticle}


@pytest.fixture
def fieldset():
    return create_fieldset_zeros_simple()


@pytest.mark.parametrize("mode", ["scipy", "jit"])
def test_metadata(fieldset, mode, tmp_zarrfile):
    pset = ParticleSet(fieldset, pclass=ptype[mode], lon=0, lat=0)

    pset.execute(DoNothing, runtime=1, output_file=pset.ParticleFile(tmp_zarrfile, outputdt=1))

    ds = xr.open_zarr(tmp_zarrfile)
    assert ds.attrs["parcels_kernels"].lower() == f"{mode}ParticleDoNothing".lower()


@pytest.mark.parametrize("mode", ["scipy", "jit"])
def test_pfile_array_write_zarr_memorystore(fieldset, mode):
    """Check that writing to a Zarr MemoryStore works."""
    npart = 10
    zarr_store = MemoryStore()
    pset = ParticleSet(fieldset, pclass=ptype[mode], lon=np.linspace(0, 1, npart), lat=0.5 * np.ones(npart), time=0)
    pfile = pset.ParticleFile(zarr_store, outputdt=1)
    pfile.write(pset, 0)

    ds = xr.open_zarr(zarr_store)
    assert ds.sizes["trajectory"] == npart
    ds.close()


@pytest.mark.parametrize("mode", ["scipy", "jit"])
def test_pfile_array_remove_particles(fieldset, mode, tmp_zarrfile):
    npart = 10
    pset = ParticleSet(fieldset, pclass=ptype[mode], lon=np.linspace(0, 1, npart), lat=0.5 * np.ones(npart), time=0)
    pfile = pset.ParticleFile(tmp_zarrfile, outputdt=1)
    pfile.write(pset, 0)
    pset.remove_indices(3)
    for p in pset:
        p.time = 1
    pfile.write(pset, 1)

    ds = xr.open_zarr(tmp_zarrfile)
    timearr = ds["time"][:]
    assert (np.isnat(timearr[3, 1])) and (np.isfinite(timearr[3, 0]))
    ds.close()


@pytest.mark.parametrize("mode", ["scipy", "jit"])
def test_pfile_set_towrite_False(fieldset, mode, tmp_zarrfile):
    npart = 10
    pset = ParticleSet(fieldset, pclass=ptype[mode], lon=np.linspace(0, 1, npart), lat=0.5 * np.ones(npart))
    pset.set_variable_write_status("depth", False)
    pset.set_variable_write_status("lat", False)
    pfile = pset.ParticleFile(tmp_zarrfile, outputdt=1)

    def Update_lon(particle, fieldset, time):  # pragma: no cover
        particle_dlon += 0.1  # noqa

    pset.execute(Update_lon, runtime=10, output_file=pfile)

    ds = xr.open_zarr(tmp_zarrfile)
    assert "time" in ds
    assert "z" not in ds
    assert "lat" not in ds
    ds.close()

    # For pytest purposes, we need to reset to original status
    pset.set_variable_write_status("depth", True)
    pset.set_variable_write_status("lat", True)


@pytest.mark.parametrize("mode", ["scipy", "jit"])
@pytest.mark.parametrize("chunks_obs", [1, None])
def test_pfile_array_remove_all_particles(fieldset, mode, chunks_obs, tmp_zarrfile):
    npart = 10
    pset = ParticleSet(fieldset, pclass=ptype[mode], lon=np.linspace(0, 1, npart), lat=0.5 * np.ones(npart), time=0)
    chunks = (npart, chunks_obs) if chunks_obs else None
    pfile = pset.ParticleFile(tmp_zarrfile, chunks=chunks, outputdt=1)
    pfile.write(pset, 0)
    for _ in range(npart):
        pset.remove_indices(-1)
    pfile.write(pset, 1)
    pfile.write(pset, 2)

    ds = xr.open_zarr(tmp_zarrfile).load()
    assert np.allclose(ds["time"][:, 0], np.timedelta64(0, "s"), atol=np.timedelta64(1, "ms"))
    if chunks_obs is not None:
        assert ds["time"][:].shape == chunks
    else:
        assert ds["time"][:].shape[0] == npart
        assert np.all(np.isnan(ds["time"][:, 1:]))
    ds.close()


@pytest.mark.parametrize("mode", ["scipy", "jit"])
def test_variable_write_double(fieldset, mode, tmp_zarrfile):
    def Update_lon(particle, fieldset, time):  # pragma: no cover
        particle_dlon += 0.1  # noqa

    pset = ParticleSet(fieldset, pclass=ptype[mode], lon=[0], lat=[0], lonlatdepth_dtype=np.float64)
    ofile = pset.ParticleFile(name=tmp_zarrfile, outputdt=0.00001)
    pset.execute(pset.Kernel(Update_lon), endtime=0.001, dt=0.00001, output_file=ofile)

    ds = xr.open_zarr(tmp_zarrfile)
    lons = ds["lon"][:]
    assert isinstance(lons.values[0, 0], np.float64)
    ds.close()


@pytest.mark.parametrize("mode", ["scipy", "jit"])
def test_write_dtypes_pfile(fieldset, mode, tmp_zarrfile):
    dtypes = [np.float32, np.float64, np.int32, np.uint32, np.int64, np.uint64]
    if mode == "scipy":
        dtypes.extend([np.bool_, np.int8, np.uint8, np.int16, np.uint16])

    extra_vars = [Variable(f"v_{d.__name__}", dtype=d, initial=0.0) for d in dtypes]
    MyParticle = ptype[mode].add_variables(extra_vars)

    pset = ParticleSet(fieldset, pclass=MyParticle, lon=0, lat=0, time=0)
    pfile = pset.ParticleFile(name=tmp_zarrfile, outputdt=1)
    pfile.write(pset, 0)

    ds = xr.open_zarr(
        tmp_zarrfile, mask_and_scale=False
    )  # Note masking issue at https://stackoverflow.com/questions/68460507/xarray-loading-int-data-as-float
    for d in dtypes:
        assert ds[f"v_{d.__name__}"].dtype == d


@pytest.mark.parametrize("mode", ["scipy", "jit"])
@pytest.mark.parametrize("npart", [1, 2, 5])
def test_variable_written_once(fieldset, mode, tmp_zarrfile, npart):
    def Update_v(particle, fieldset, time):  # pragma: no cover
        particle.v_once += 1.0
        particle.age += particle.dt

    MyParticle = ptype[mode].add_variables(
        [
            Variable("v_once", dtype=np.float64, initial=0.0, to_write="once"),
            Variable("age", dtype=np.float32, initial=0.0),
        ]
    )
    lon = np.linspace(0, 1, npart)
    lat = np.linspace(1, 0, npart)
    time = np.arange(0, npart / 10.0, 0.1, dtype=np.float64)
    pset = ParticleSet(fieldset, pclass=MyParticle, lon=lon, lat=lat, time=time, v_once=time)
    ofile = pset.ParticleFile(name=tmp_zarrfile, outputdt=0.1)
    pset.execute(pset.Kernel(Update_v), endtime=1, dt=0.1, output_file=ofile)

    assert np.allclose(pset.v_once - time - pset.age * 10, 1, atol=1e-5)
    ds = xr.open_zarr(tmp_zarrfile)
    vfile = np.ma.filled(ds["v_once"][:], np.nan)
    assert vfile.shape == (npart,)
    ds.close()


@pytest.mark.parametrize("type", ["repeatdt", "timearr"])
@pytest.mark.parametrize("mode", ["scipy", "jit"])
@pytest.mark.parametrize("repeatdt", range(1, 3))
@pytest.mark.parametrize("dt", [-1, 1])
@pytest.mark.parametrize("maxvar", [2, 4, 10])
def test_pset_repeated_release_delayed_adding_deleting(type, fieldset, mode, repeatdt, tmp_zarrfile, dt, maxvar):
    runtime = 10
    fieldset.maxvar = maxvar
    pset = None

    MyParticle = ptype[mode].add_variables(
        [Variable("sample_var", initial=0.0), Variable("v_once", dtype=np.float64, initial=0.0, to_write="once")]
    )

    if type == "repeatdt":
        pset = ParticleSet(fieldset, lon=[0], lat=[0], pclass=MyParticle, repeatdt=repeatdt)
    elif type == "timearr":
        pset = ParticleSet(
            fieldset, lon=np.zeros(runtime), lat=np.zeros(runtime), pclass=MyParticle, time=list(range(runtime))
        )
    pfile = pset.ParticleFile(tmp_zarrfile, outputdt=abs(dt), chunks=(1, 1))

    def IncrLon(particle, fieldset, time):  # pragma: no cover
        particle.sample_var += 1.0
        if particle.sample_var > fieldset.maxvar:
            particle.delete()

    for _ in range(runtime):
        pset.execute(IncrLon, dt=dt, runtime=1.0, output_file=pfile)

    ds = xr.open_zarr(tmp_zarrfile)
    samplevar = ds["sample_var"][:]
    if type == "repeatdt":
        assert samplevar.shape == (runtime // repeatdt, min(maxvar + 1, runtime))
        assert np.allclose(pset.sample_var, np.arange(maxvar, -1, -repeatdt))
    elif type == "timearr":
        assert samplevar.shape == (runtime, min(maxvar + 1, runtime))
    # test whether samplevar[:, k] = k
    for k in range(samplevar.shape[1]):
        assert np.allclose([p for p in samplevar[:, k] if np.isfinite(p)], k + 1)
    filesize = os.path.getsize(str(tmp_zarrfile))
    assert filesize < 1024 * 65  # test that chunking leads to filesize less than 65KB
    ds.close()


@pytest.mark.parametrize("mode", ["scipy", "jit"])
@pytest.mark.parametrize("repeatdt", [1, 2])
@pytest.mark.parametrize("nump", [1, 10])
def test_pfile_chunks_repeatedrelease(fieldset, mode, repeatdt, nump, tmp_zarrfile):
    runtime = 8
    pset = ParticleSet(
        fieldset, pclass=ptype[mode], lon=np.zeros((nump, 1)), lat=np.zeros((nump, 1)), repeatdt=repeatdt
    )
    chunks = (20, 10)
    pfile = pset.ParticleFile(tmp_zarrfile, outputdt=1, chunks=chunks)

    def DoNothing(particle, fieldset, time):  # pragma: no cover
        pass

    pset.execute(DoNothing, dt=1, runtime=runtime, output_file=pfile)
    ds = xr.open_zarr(tmp_zarrfile)
    assert ds["time"].shape == (int(nump * runtime / repeatdt), chunks[1])


@pytest.mark.parametrize("mode", ["scipy", "jit"])
def test_write_timebackward(fieldset, mode, tmp_zarrfile):
    def Update_lon(particle, fieldset, time):  # pragma: no cover
        particle_dlon -= 0.1 * particle.dt  # noqa

    pset = ParticleSet(fieldset, pclass=ptype[mode], lat=np.linspace(0, 1, 3), lon=[0, 0, 0], time=[1, 2, 3])
    pfile = pset.ParticleFile(name=tmp_zarrfile, outputdt=1.0)
    pset.execute(pset.Kernel(Update_lon), runtime=4, dt=-1.0, output_file=pfile)
    ds = xr.open_zarr(tmp_zarrfile)
    trajs = ds["trajectory"][:]
    assert trajs.values.dtype == "int64"
    assert np.all(np.diff(trajs.values) < 0)  # all particles written in order of release
    ds.close()


@pytest.mark.parametrize("mode", ["scipy", "jit"])
def test_write_xiyi(fieldset, mode, tmp_zarrfile):
    fieldset.U.data[:] = 1  # set a non-zero zonal velocity
    fieldset.add_field(Field(name="P", data=np.zeros((3, 20)), lon=np.linspace(0, 1, 20), lat=[-2, 0, 2]))
    dt = 3600

    XiYiParticle = ptype[mode].add_variables(
        [
            Variable("pxi0", dtype=np.int32, initial=0.0),
            Variable("pxi1", dtype=np.int32, initial=0.0),
            Variable("pyi", dtype=np.int32, initial=0.0),
        ]
    )

    def Get_XiYi(particle, fieldset, time):  # pragma: no cover
        """Kernel to sample the grid indices of the particle.
        Note that this sampling should be done _before_ the advection kernel
        and that the first outputted value is zero.
        Be careful when using multiple grids, as the index may be different for the grids.
        """
        particle.pxi0 = particle.xi[0]
        particle.pxi1 = particle.xi[1]
        particle.pyi = particle.yi[0]

    def SampleP(particle, fieldset, time):  # pragma: no cover
        if time > 5 * 3600:
            _ = fieldset.P[particle]  # To trigger sampling of the P field

    pset = ParticleSet(fieldset, pclass=XiYiParticle, lon=[0, 0.2], lat=[0.2, 1], lonlatdepth_dtype=np.float64)
    pfile = pset.ParticleFile(name=tmp_zarrfile, outputdt=dt)
    pset.execute([SampleP, Get_XiYi, AdvectionRK4], endtime=10 * dt, dt=dt, output_file=pfile)

    ds = xr.open_zarr(tmp_zarrfile)
    pxi0 = ds["pxi0"][:].values.astype(np.int32)
    pxi1 = ds["pxi1"][:].values.astype(np.int32)
    lons = ds["lon"][:].values
    pyi = ds["pyi"][:].values.astype(np.int32)
    lats = ds["lat"][:].values

    for p in range(pyi.shape[0]):
        assert (pxi0[p, 0] == 0) and (pxi0[p, -1] == pset[p].pxi0)  # check that particle has moved
        assert np.all(pxi1[p, :6] == 0)  # check that particle has not been sampled on grid 1 until time 6
        assert np.all(pxi1[p, 6:] > 0)  # check that particle has not been sampled on grid 1 after time 6
        for xi, lon in zip(pxi0[p, 1:], lons[p, 1:], strict=True):
            assert fieldset.U.grid.lon[xi] <= lon < fieldset.U.grid.lon[xi + 1]
        for xi, lon in zip(pxi1[p, 6:], lons[p, 6:], strict=True):
            assert fieldset.P.grid.lon[xi] <= lon < fieldset.P.grid.lon[xi + 1]
        for yi, lat in zip(pyi[p, 1:], lats[p, 1:], strict=True):
            assert fieldset.U.grid.lat[yi] <= lat < fieldset.U.grid.lat[yi + 1]
    ds.close()


def test_set_calendar():
    for _calendar_name, cf_datetime in zip(_get_cftime_calendars(), _get_cftime_datetimes(), strict=True):
        date = getattr(cftime, cf_datetime)(1990, 1, 1)
        assert _set_calendar(date.calendar) == date.calendar
    assert _set_calendar("np_datetime64") == "standard"


@pytest.mark.parametrize("mode", ["scipy", "jit"])
def test_reset_dt(fieldset, mode, tmp_zarrfile):
    # Assert that p.dt gets reset when a write_time is not a multiple of dt
    # for p.dt=0.02 to reach outputdt=0.05 and endtime=0.1, the steps should be [0.2, 0.2, 0.1, 0.2, 0.2, 0.1], resulting in 6 kernel executions

    def Update_lon(particle, fieldset, time):  # pragma: no cover
        particle_dlon += 0.1  # noqa

    pset = ParticleSet(fieldset, pclass=ptype[mode], lon=[0], lat=[0], lonlatdepth_dtype=np.float64)
    ofile = pset.ParticleFile(name=tmp_zarrfile, outputdt=0.05)
    pset.execute(pset.Kernel(Update_lon), endtime=0.12, dt=0.02, output_file=ofile)

    assert np.allclose(pset.lon, 0.6)


@pytest.mark.parametrize("mode", ["scipy", "jit"])
def test_correct_misaligned_outputdt_dt(fieldset, mode, tmp_zarrfile):
    """Testing that outputdt does not need to be a multiple of dt."""

    def Update_lon(particle, fieldset, time):  # pragma: no cover
        particle_dlon += particle.dt  # noqa

    pset = ParticleSet(fieldset, pclass=ptype[mode], lon=[0], lat=[0], lonlatdepth_dtype=np.float64)
    ofile = pset.ParticleFile(name=tmp_zarrfile, outputdt=3)
    pset.execute(pset.Kernel(Update_lon), endtime=11, dt=2, output_file=ofile)

    ds = xr.open_zarr(tmp_zarrfile)
    assert np.allclose(ds.lon.values, [0, 3, 6, 9])
    assert np.allclose(
        ds.time.values[0, :], [np.timedelta64(t, "s") for t in [0, 3, 6, 9]], atol=np.timedelta64(1, "ns")
    )


def setup_pset_execute(*, fieldset: FieldSet, outputdt: timedelta, execute_kwargs, particle_class=ScipyParticle):
    npart = 10

    if fieldset is None:
        fieldset = create_fieldset_zeros_simple()

    pset = ParticleSet(
        fieldset,
        pclass=particle_class,
        lon=np.full(npart, fieldset.U.lon.mean()),
        lat=np.full(npart, fieldset.U.lat.mean()),
    )

    with tempfile.TemporaryDirectory() as dir:
        name = f"{dir}/test.zarr"
        output_file = pset.ParticleFile(name=name, outputdt=outputdt)

        pset.execute(DoNothing, output_file=output_file, **execute_kwargs)
        ds = xr.open_zarr(name).load()

    return ds


def test_pset_execute_outputdt_forwards():
    """Testing output data dt matches outputdt in forward time."""
    outputdt = timedelta(hours=1)
    runtime = timedelta(hours=5)
    dt = timedelta(minutes=5)

    ds = setup_pset_execute(
        fieldset=create_fieldset_zeros_simple(), outputdt=outputdt, execute_kwargs=dict(runtime=runtime, dt=dt)
    )

    assert np.all(ds.isel(trajectory=0).time.diff(dim="obs").values == np.timedelta64(outputdt))


def test_pset_execute_outputdt_backwards():
    """Testing output data dt matches outputdt in backwards time."""
    outputdt = timedelta(hours=1)
    runtime = timedelta(days=2)
    dt = -timedelta(minutes=5)

    ds = setup_pset_execute(
        fieldset=create_fieldset_zeros_simple(), outputdt=outputdt, execute_kwargs=dict(runtime=runtime, dt=dt)
    )
    file_outputdt = ds.isel(trajectory=0).time.diff(dim="obs").values
    assert np.all(file_outputdt == np.timedelta64(-outputdt))


def test_pset_execute_outputdt_backwards_fieldset_timevarying():
    """test_pset_execute_outputdt_backwards() still passed despite #1722 as it doesn't account for time-varying fields,
    which for some reason #1722
    """
    outputdt = timedelta(hours=1)
    runtime = timedelta(days=2)
    dt = -timedelta(minutes=5)

    # TODO: Not ideal using the `download_example_dataset` here, but I'm struggling to recreate this error using the test suite fieldsets we have
    example_dataset_folder = parcels.download_example_dataset("MovingEddies_data")
    fieldset = parcels.FieldSet.from_parcels(f"{example_dataset_folder}/moving_eddies")

    ds = setup_pset_execute(outputdt=outputdt, execute_kwargs=dict(runtime=runtime, dt=dt), fieldset=fieldset)
    file_outputdt = ds.isel(trajectory=0).time.diff(dim="obs").values
    assert np.all(file_outputdt == np.timedelta64(-outputdt)), (file_outputdt, np.timedelta64(-outputdt))
