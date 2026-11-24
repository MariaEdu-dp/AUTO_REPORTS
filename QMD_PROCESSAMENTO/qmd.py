import xarray as xr
import numpy as np
from dask.diagnostics import ProgressBar
import os  # <--- para checar arquivos

# -------------------------------
# 1. Load dataset lazily
# -------------------------------
dt = xr.open_dataset(r"C:\FACULDADE_2\WEC\DADOS\QUEIMADAS\MOD14A2.061_1km_2025.nc")["FireMask"]
print(dt)

# Rechunk for better performance
dt = dt.chunk({"time": 1, "lat": 2000, "lon": 2000})
print(dt)

base_grid = xr.open_dataset(r"C:\Users\maedp\PycharmProjects\WEC\QMD_PROCESSAMENTO\firecounts_2024_0.1deg_monthly_TERRA.nc")

# -------------------------------
# 2. Compute coarsening factors for spatial aggregation
# -------------------------------
lat_res = float(np.abs(dt.lat[1] - dt.lat[0]))
lon_res = float(np.abs(dt.lon[1] - dt.lon[0]))
target_res = 0.1
lat_factor = int(target_res / lat_res)
lon_factor = int(target_res / lon_res)

# -------------------------------
# 3. Loop over years and process each year separately
# -------------------------------
years = np.arange(dt.time.dt.year.min().item(), dt.time.dt.year.max().item() + 1)

for year in years:
    output_file = f"firecounts_{year}_0.1deg_monthly_TERRA.nc"

    # Verifica se o arquivo já existe
    if os.path.exists(output_file):
        print(f"File {output_file} already exists. Skipping year {year}.")
        continue

    print(f"\nProcessing year {year} ...")

    # Slice the year
    dt_year = dt.sel(time=slice(f"{year}-01-01", f"{year}-12-31"))

    # Lazy binarization: 1 for fires > 7, 0 otherwise
    dt_year_bin = (dt_year > 7).astype(np.int8)

    # Lazy spatial aggregation to 0.1°
    dt_year_coarse = dt_year_bin.coarsen(
        lat=lat_factor, lon=lon_factor, boundary="trim"
    ).sum()

    # Lazy temporal aggregation to monthly sums
    dt_year_monthly = dt_year_coarse.resample(time="ME").sum()

    print(dt_year_monthly)

    # Compute and save this year's file with a progress bar
    with ProgressBar():
        dt_year_monthly.to_netcdf(output_file)

    print(f"Year {year} done! Saved to {output_file}")

print(base_grid)
