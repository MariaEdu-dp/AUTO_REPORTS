"""Generate the institution's quarterly South America fire-monitoring bulletin.

Edit the USER CONFIGURATION section only when changing machines, data sources,
Earth Engine project, report period, visual style or output files. Page data
preparation and page drawing are labeled separately below.

The script generates one PDF per (year, season) pair. The default batch at the
bottom of this file produces MAM, JJA and SON for 2004 through 2025 — 66 reports.
Edit `REPORT_CONFIGS` to change the batch. The "Volume/Nº/Month" header line
is derived automatically from each (year, season) pair.

DJF is fully supported by the internal processing (season_year groups
Dec(Y-1) + Jan(Y) + Feb(Y) together). It is excluded from the default batch
only to keep the initial run manageable. Add "DJF" to BATCH_SEASONS to include
it; the page-1 inset automatically switches to the 12-month window ending with
the season, and the fire map sums the correct three months.

Per report, only protected areas that had already been created by the report
year are counted:
  - conservation units: `ANO_CRIA6`   (year of creation)
  - indigenous lands:   `creat_year`  (year of homologation)
Entries with a missing creation year are kept by default (documented inline).
"""

from pathlib import Path
import glob
import os
import textwrap
import time
from datetime import datetime

import cartopy.crs as ccrs
import cartopy.feature as cfeature
import ee
import geopandas as gpd
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
import rioxarray
import xarray as xr
from matplotlib import font_manager as fm
from matplotlib.backends.backend_pdf import PdfPages
from matplotlib.colors import LinearSegmentedColormap
from matplotlib.dates import MonthLocator, DateFormatter
from matplotlib.ticker import ScalarFormatter
from pdf2image import convert_from_path
from pypdf import PdfReader, PdfWriter
from rasterio.features import rasterize as rio_rasterize
from shapely.affinity import rotate
from shapely.geometry import LineString, Point, Polygon

# =============================================================================
# USER CONFIGURATION — edit this section before running the bulletin elsewhere
# =============================================================================
PATHS = {
    "fire_data_dir": Path(r"../QMD_PROCESSAMENTO/"),
    "countries": Path(r"DATA/SHP/Countries/Lim_america_do_sul_2021.shp"),
    "biomes": Path(r"DATA/SHP/Biomes/DISSOLVIDO_BIOMA_FINAL.gpkg"),
    "biome_colors": Path(r"DATA/SHP/Biomes/OLSON_WWF_BIOMAS_FINAL_ING.clr"),
    "municipalities": Path(r"DATA/SHP/Municipalities/BR_Municipios_2024.shp"),
    "indigenous_lands": Path(r"DATA/SHP/Indigenous_lands/tis_poligonais_homologadas.shp"),
    "conservation_units": Path(r"DATA/SHP/Conservation_Units/GEOFT_UNIDADE_CONSERVACAO.shp"),
    "header_pdf": Path(r"DATA/REPORT_HEADER.pdf"),
    "reference_maps": Path(r"DATA/REF_MAPS.png"),
    "font_header": Path("DATA/FONTS/Libre_Baskerville/LibreBaskerville-Bold.ttf"),
    "font_body": Path(r"DATA/FONTS/Montserrat/Montserrat-Regular.ttf"),
}

IUCN_RASTERS = {
    "Amphibians": Path(r"DATA/TIF/IUCN/Amphibians_RWR_2025.tif"),
    "Birds": Path(r"DATA/TIF/IUCN/Birds_RWR_2025.tif"),
    "Mammals": Path(r"DATA/TIF/IUCN/Mammals_RWR_2025.tif"),
    "Reptiles": Path(r"DATA/TIF/IUCN/Reptiles_RWR_2025.tif"),
}

FIRE_ALL_PATTERN = str(PATHS["fire_data_dir"] / "*.nc")
ALIGNED_FIRE_PATTERN = str(PATHS["fire_data_dir"] / "*TERRA.nc")

EE_PROJECT = "ee-maedppro-ufsj"
REPORT_TITLE = "FIRE MONITORING"
SOURCE_INFO = "Main source: MOD14A1"

# -----------------------------------------------------------------------------
# BATCH CONFIGURATION — which (year, season) pairs to generate
# -----------------------------------------------------------------------------
# DJF is fully supported by the processing; add "DJF" to BATCH_SEASONS to
# include it. The current default is MAM/JJA/SON for 2004–2025 (66 reports).
BATCH_YEARS = list(range(2025, 2026))  # 2004..2025 inclusive (22 years)
BATCH_SEASONS = ["DJF"]
REPORT_CONFIGS = [(y, s) for y in BATCH_YEARS for s in BATCH_SEASONS]

SCRIPT_DIR = Path(__file__).resolve().parent

PAGE_WIDTH_PX, PAGE_HEIGHT_PX = 5000, 6000
DPI = 550
NUMBER_OF_PAGES = 7
MAP_EXTENT = [-84, -32, -59, 14]
GEE_REGION_BOUNDS = [-85, -60, -24, 15]

COUNTRY_BAR_COLORS = ["#828282", "#aba138", "#ba4411", "#d1d4a7"]
ET_COLORS = ["#f5f5f5", "#dbc9ac", "#e6a817", "#42b89c"]
RAIN_COLORS = ["#e0e0e0", "#0d47a1"]
SMOKE_COLORS = ["#ffffff", "#bdbdbd", "#ffd54f", "#f57c00"]


# =============================================================================
# SHARED HELPERS
# =============================================================================
def _font_properties(path, family, weight=None):
    if path.is_file():
        fm.fontManager.addfont(str(path))
        return fm.FontProperties(fname=str(path))
    print(f"Font not found: {path}. Using the installed {family} fallback.")
    return fm.FontProperties(family=family, weight=weight)


HEADER_FONT = _font_properties(PATHS["font_header"], "serif", "bold")
BODY_FONT = _font_properties(PATHS["font_body"], "sans-serif")
plt.rcParams["font.family"] = "Montserrat" if PATHS["font_body"].is_file() else "sans-serif"


def _build_report_authors(year, season):
    """Return the 'Volume NN, NºNN, Mon/YYYY' string for a given report."""
    season_numbers = {"DJF": 1, "MAM": 2, "JJA": 3, "SON": 4}
    month_after = {"DJF": "Mar", "MAM": "Jun", "JJA": "Sep", "SON": "Dec"}
    volume = year - 2000  # 2001 -> Volume 01, 2025 -> Volume 25
    number = season_numbers[season.upper()]
    month = month_after[season.upper()]
    return f"Volume {volume:02d}, Nº{number:02d}, {month}/{year}"


def _season_year(da, season):
    """Return a DataArray of season-year labels, named 'year'.

    For MAM/JJA/SON this is identical to `time.dt.year`.
    For DJF, December of Y-1 is assigned to season year Y, so
    Dec(Y-1) + Jan(Y) + Feb(Y) all share the same label. This matches
    the GEE window returned by `get_season_dates`.
    """
    yr = da["time"].dt.year
    if season.upper() == "DJF":
        mo = da["time"].dt.month
        return yr.where(mo != 12, yr + 1).rename("year")
    return yr.rename("year")


def draw_header(fig, header_title, report_authors):
    fig.text(0.05, 0.95, header_title.upper(), fontsize=18, weight="bold",
             va="top", fontproperties=HEADER_FONT)
    fig.text(0.7125, 0.945, report_authors, fontsize=12, va="top",
             fontproperties=BODY_FONT)
    fig.add_artist(plt.Line2D([0.05, 0.95], [0.90, 0.90], linewidth=1, color="#878787"))


def get_season_dates(year, season):
    season = season.upper()
    season_map = {
        "DJF": {"name": "summer", "months": (12, 1, 2)},
        "MAM": {"name": "autumn", "months": (3, 4, 5)},
        "JJA": {"name": "winter", "months": (6, 7, 8)},
        "SON": {"name": "spring", "months": (9, 10, 11)},
    }
    if season not in season_map:
        raise ValueError("Season must be DJF, MAM, JJA or SON")
    season_name = season_map[season]["name"]
    m1, m2, m3 = season_map[season]["months"]
    if season == "DJF":
        start_date = datetime(year - 1, 12, 1)
        end_date = datetime(year, 2, 28)
    else:
        start_date = datetime(year, m1, 1)
        if m3 in [1, 3, 5, 7, 8, 10, 12]:
            last_day = 31
        elif m3 in [4, 6, 9, 11]:
            last_day = 30
        else:
            last_day = 28
        end_date = datetime(year, m3, last_day)
    return start_date.strftime("%Y-%m-%d"), end_date.strftime("%Y-%m-%d"), season_name


def wrap_labels(labels, width=15):
    return ['\n'.join(textwrap.wrap(label, width)) for label in labels]


def wrap_text(text, width=45):
    return '\n'.join(textwrap.wrap(text, width))


def create_directional_sectors(polygon, n_sectors=8):
    if polygon is None or polygon.is_empty:
        return [], []
    centroid = polygon.centroid
    bounds = polygon.bounds
    max_dist = max(bounds[2] - bounds[0], bounds[3] - bounds[1]) * 1.5
    east_point = Point(centroid.x + max_dist, centroid.y)
    sector_angle = 360 / n_sectors
    sectors = []
    dir_names = []
    for i in range(n_sectors):
        angle = i * sector_angle
        line = LineString([centroid, east_point])
        rotated_line = rotate(line, angle, origin=centroid, use_radians=False)
        next_line = rotate(line, angle + sector_angle, origin=centroid, use_radians=False)
        p1 = rotated_line.coords[1]
        p2 = next_line.coords[1]
        wedge = Polygon([centroid, p1, p2, centroid])
        sector = wedge.intersection(polygon)
        if not sector.is_empty:
            sectors.append(sector)
            if 0 <= angle < 45:
                dir_names.append('northeast')
            elif 45 <= angle < 90:
                dir_names.append('north')
            elif 90 <= angle < 135:
                dir_names.append('northwest')
            elif 135 <= angle < 180:
                dir_names.append('west')
            elif 180 <= angle < 225:
                dir_names.append('southwest')
            elif 225 <= angle < 270:
                dir_names.append('south')
            elif 270 <= angle < 315:
                dir_names.append('southeast')
            else:
                dir_names.append('east')
    return sectors, dir_names


def sum_fires_per_polygon(gdf, raster_da):
    """Return [(name, fire_sum), ...] for polygons with fires > 0.

    Uses rasterio.features.rasterize + np.bincount, which does NOT allocate
    per-polygon windows (unlike rasterstats) and therefore never trips the
    'negative dimensions' error. Both the GeoDataFrame and the raster are
    assumed to share the raster's CRS (EPSG:4674 in this bulletin).
    """
    raster = raster_da.sortby("lat", ascending=False).sortby("lon").load()
    raster = raster.rio.write_crs("EPSG:4674")
    values = np.array(raster.values, dtype=np.float64, copy=True)
    values[~np.isfinite(values)] = 0.0
    transform = raster.rio.transform()

    gdf = gdf[gdf.geometry.notna() & ~gdf.geometry.is_empty].copy()
    bad = ~gdf.geometry.is_valid
    if bad.any():
        gdf.loc[bad, "geometry"] = gdf.loc[bad, "geometry"].buffer(0)
    gdf = gdf[gdf.geometry.notna() & ~gdf.geometry.is_empty]
    if len(gdf) == 0:
        return []

    shapes = [(geom, i + 1) for i, geom in enumerate(gdf.geometry)]
    labels = rio_rasterize(
        shapes,
        out_shape=values.shape,
        transform=transform,
        fill=0,
        all_touched=False,
        dtype="int32",
    )

    sums = np.bincount(
        labels.ravel(),
        weights=values.ravel(),
        minlength=len(gdf) + 1,
    )
    return [
        (name, float(sums[i + 1]))
        for i, name in enumerate(gdf["name"])
        if sums[i + 1] > 0
    ]


# =============================================================================
# REPORT GENERATION
# =============================================================================
def generate_report(year, season):
    """Generate one bulletin for the given (year, season)."""
    season = season.upper()
    report_authors = _build_report_authors(year, season)

    print(f"  Report header: {report_authors}")

    pages = NUMBER_OF_PAGES
    dpi = DPI
    W_px, H_px = PAGE_WIDTH_PX, PAGE_HEIGHT_PX
    figsize = (W_px / dpi, H_px / dpi)
    extent = MAP_EXTENT
    PC_PROJ = ccrs.PlateCarree()
    report_title = REPORT_TITLE
    source_info = SOURCE_INFO
    formatter = ScalarFormatter(useMathText=True)
    formatter.set_scientific(True)
    formatter.set_powerlimits((0, 0))
    colors = COUNTRY_BAR_COLORS
    et_cmap = LinearSegmentedColormap.from_list("et_palette", ET_COLORS, N=256)
    rain_cmap = LinearSegmentedColormap.from_list("rain_palette", RAIN_COLORS, N=256)
    smoke_cmap = LinearSegmentedColormap.from_list("smoke_palette", SMOKE_COLORS, N=256)
    start_date, end_date, season_name = get_season_dates(year, season)
    region = ee.Geometry.Rectangle(GEE_REGION_BOUNDS)

    output_pdf = SCRIPT_DIR / "miolo_temp.pdf"
    final_output_pdf = SCRIPT_DIR / f"report_{year}_{season}.pdf"
    temp_cover_page_pdf = SCRIPT_DIR / "temp_page.pdf"

    required_paths = [
        PATHS["countries"], PATHS["biomes"], PATHS["biome_colors"],
        PATHS["municipalities"], PATHS["indigenous_lands"], PATHS["conservation_units"],
        PATHS["header_pdf"], PATHS["reference_maps"], *IUCN_RASTERS.values(),
    ]
    missing_paths = [str(path) for path in required_paths if not path.is_file()]
    if missing_paths:
        missing_text = "\n".join(f"  - {path}" for path in missing_paths)
        raise FileNotFoundError(
            "These input files were not found. Update PATHS/IUCN_RASTERS in USER CONFIGURATION:\n"
            + missing_text
        )
    if not glob.glob(FIRE_ALL_PATTERN):
        raise FileNotFoundError(f"No fire NetCDF files matched: {FIRE_ALL_PATTERN}")

    # ================================================================
    # PAGE 1 — PREPARE FIRE TOTALS, COUNTRY COMPARISONS AND MAP FIELDS
    # ================================================================
    fires = xr.open_mfdataset(FIRE_ALL_PATTERN)["FireMask"]
    fires = fires.rio.set_spatial_dims(x_dim="lon", y_dim="lat")
    fires = fires.rio.write_crs("EPSG:4674")

    countries = gpd.read_file(str(PATHS["countries"]), engine="pyogrio").to_crs("EPSG:4674")
    sa_union = countries.unary_union

    total_sum = fires.sum(["lat", "lon"])

    # -----------------------------------------------------------------
    # DJF fix: group by season year (Dec Y-1 + Jan Y + Feb Y -> Y).
    # For MAM/JJA/SON this is identical to `.groupby("time.year")`.
    # -----------------------------------------------------------------
    fires_season = fires.where(fires["time.season"] == season, drop=True)
    season_ts = fires_season.groupby(_season_year(fires_season, season))

    available_years = np.sort(np.unique(fires["time.year"].values))
    available_years = available_years[available_years <= year]
    last_years = available_years[-4:]
    print("Comparison years:", last_years)

    if year not in available_years:
        raise ValueError(
            f"No fire data for year {year} ({season}). "
            f"Available years: {available_years.tolist()}"
        )

    print("1. Processing countries...")
    country_year_data = {}
    for idx, row in countries.iterrows():
        country_name = row["nome"]
        geom = [row.geometry]
        print(f"    Processing {country_name}...")
        try:
            clipped = fires.rio.clip(geom, countries.crs, drop=True)
            clipped_season = clipped.where(clipped["time.season"] == season, drop=True)
            seasonal_country = (
                clipped_season
                .groupby(_season_year(clipped_season, season))
                .sum(dim=["time", "lat", "lon"])
            )
            seasonal_country = seasonal_country.sel(year=last_years)
            country_year_data[country_name] = seasonal_country.values
        except Exception:
            country_year_data[country_name] = np.zeros(len(last_years))

    df = pd.DataFrame(country_year_data, index=last_years)
    df_long = df.reset_index().melt(id_vars="index", var_name="Country", value_name="Fires")
    df_long = df_long.rename(columns={"index": "Year"})
    latest_year = last_years[-1]
    top_countries = df.loc[latest_year].sort_values(ascending=False).head(4).index
    df_long = df_long[df_long["Country"].isin(top_countries)]
    df_plot = df_long.pivot(index="Country", columns="Year", values="Fires")
    df_plot = df_plot.apply(pd.to_numeric, errors="coerce").fillna(0)
    current_year = year
    previous_year = current_year - 1
    top_country = df_plot[current_year].idxmax()
    top_value = df_plot[current_year].max()
    if previous_year in df_plot.columns:
        diff = df_plot[current_year] - df_plot[previous_year]
        inc_country = diff.abs().idxmax()
        inc_value = diff.loc[inc_country]
    else:
        inc_country = None

    season_all_years = season_ts.sum(dim=["time", "lat", "lon"])
    season_mean = season_all_years.mean(dim="year")
    season_year = season_all_years.sel(year=year)
    season_year_value = float(season_year.values)
    season_mean_value = float(season_mean.values)
    pct_diff = ((season_year_value - season_mean_value) / season_mean_value) * 100

    fire_presence = fires.sum("time")
    valid_pixels = fire_presence > 0
    fires = fires.where(valid_pixels)

    season_data = fires.where(fires["time.season"] == season, drop=True)
    season_yearly_maps = season_data.groupby(_season_year(season_data, season)).sum("time")
    season_yearly_maps = season_yearly_maps.where(season_yearly_maps > 0)
    season_yearly_maps = season_yearly_maps.rio.write_crs("EPSG:4674")

    print("Materializing seasonal fire maps (Dask compute, one-off)...")
    season_yearly_maps = season_yearly_maps.load()

    map_current = season_yearly_maps.sel(year=current_year)
    map_current = map_current.rio.write_crs("EPSG:4674")
    map_current = map_current.load()
    print("map_current loaded and ready for zonal statistics.")

    print("Creating aligned fire dataset for plotting...")
    files = sorted(glob.glob(ALIGNED_FIRE_PATTERN))
    if not files:
        raise FileNotFoundError(f"No aligned fire-count NetCDF files matched: {ALIGNED_FIRE_PATTERN}")
    datasets = []
    for i, f in enumerate(files):
        ds = xr.open_dataset(f)["FireMask"].sortby("lat")
        if i == 0:
            ref_lat = ds.lat
            ref_lon = ds.lon
        else:
            ds = ds.assign_coords(lat=ref_lat, lon=ref_lon)
        datasets.append(ds)

    fires_align = xr.concat(datasets, dim="time", join="exact")
    fires_align = fires_align.rio.set_spatial_dims(x_dim="lon", y_dim="lat")
    fires_align = fires_align.rio.write_crs("EPSG:4674")
    fires_align = fires_align.rio.clip([sa_union], countries.crs, drop=True)

    fires_season_align = fires_align.where(fires_align["time.season"] == season, drop=True)
    # DJF fix: pick Dec(Y-1) + Jan(Y) + Feb(Y) explicitly, not Jan-Dec(Y).
    if season == "DJF":
        yr_align = fires_season_align["time"].dt.year
        mo_align = fires_season_align["time"].dt.month
        djf_mask = ((yr_align == year - 1) & (mo_align == 12)) | \
                   ((yr_align == year) & mo_align.isin([1, 2]))
        fire_current_align = fires_season_align.where(djf_mask, drop=True).sum("time")
    else:
        fire_current_align = fires_season_align.sel(time=str(year)).sum("time")
    fire_current_align = fire_current_align.where(fire_current_align > 0)
    fire_current_align = fire_current_align.rio.write_crs("EPSG:4674")

    # ================================================================
    # PAGE 2 — PREPARE MUNICIPALITY, INDIGENOUS-LAND AND CONSERVATION-UNIT RESULTS
    # ================================================================
    print("Loading municipalities...")
    muni_gdf = gpd.read_file(str(PATHS["municipalities"]), engine="pyogrio").to_crs("EPSG:4674")
    muni_gdf['name'] = muni_gdf['NM_MUN'] + ' - ' + muni_gdf['SIGLA_UF']

    # ----- Indigenous lands: filter by `creat_year` <= report year -----
    print("Loading indigenous lands...")
    indig_gdf = gpd.read_file(str(PATHS["indigenous_lands"]), engine="pyogrio").to_crs("EPSG:4674")
    indig_gdf['name'] = indig_gdf['terrai_nom']
    if 'creat_year' in indig_gdf.columns:
        indig_gdf['creation_year'] = pd.to_numeric(indig_gdf['creat_year'], errors='coerce')
        n_before = len(indig_gdf)
        n_missing = indig_gdf['creation_year'].isna().sum()
        # Keep lands whose creation year is unknown OR <= report year.
        # To exclude lands with unknown creation year, drop the `.isna()` clause.
        indig_gdf = indig_gdf[
            indig_gdf['creation_year'].isna()
            | (indig_gdf['creation_year'] <= year)
        ].copy()
        print(f"    Filtered by 'creat_year' <= {year}: kept {len(indig_gdf)}/{n_before} "
              f"({n_missing} with no creation year kept by default).")
    else:
        print("    WARNING: 'creat_year' column not found — using all indigenous lands.")

    # ----- Conservation units: filter by `ANO_CRIA6` <= report year -----
    print("Loading conservation units...")
    cons_gdf = gpd.read_file(str(PATHS["conservation_units"]), engine="pyogrio").to_crs("EPSG:4674")
    cons_gdf['name'] = cons_gdf['NOME_UC1']
    if 'ANO_CRIA6' in cons_gdf.columns:
        cons_gdf['creation_year'] = pd.to_numeric(cons_gdf['ANO_CRIA6'], errors='coerce')
        n_before = len(cons_gdf)
        n_missing = cons_gdf['creation_year'].isna().sum()
        # Keep units whose creation year is unknown OR <= report year.
        # To exclude units with unknown creation year, drop the `.isna()` clause.
        cons_gdf = cons_gdf[
            cons_gdf['creation_year'].isna()
            | (cons_gdf['creation_year'] <= year)
        ].copy()
        print(f"    Filtered by 'ANO_CRIA6' <= {year}: kept {len(cons_gdf)}/{n_before} "
              f"({n_missing} with no creation year kept by default).")
    else:
        print("    WARNING: 'ANO_CRIA6' column not found — using all conservation units.")

    print("4. Summing fires per municipality (rasterize + bincount)...")
    muni_fires = sum_fires_per_polygon(muni_gdf, map_current)
    muni_fires.sort(key=lambda x: x[1], reverse=True)
    top_muni = muni_fires[:5]
    top_muni_names = [m[0] for m in top_muni]
    top_muni_values = [m[1] for m in top_muni]
    top_muni_states = [name.split(' - ')[-1] for name in top_muni_names]

    print("5. Summing fires per indigenous land (rasterize + bincount)...")
    indig_fires = sum_fires_per_polygon(indig_gdf, map_current)
    indig_fires.sort(key=lambda x: x[1], reverse=True)
    top_indig = indig_fires[:5]
    top_indig_names = [i[0] for i in top_indig]
    top_indig_values = [i[1] for i in top_indig]

    print("6. Summing fires per conservation unit (rasterize + bincount)...")
    cons_fires = sum_fires_per_polygon(cons_gdf, map_current)
    cons_fires.sort(key=lambda x: x[1], reverse=True)
    top_cons = cons_fires[:5]
    top_cons_names = [c[0] for c in top_cons]
    top_cons_values = [c[1] for c in top_cons]

    muni_codes = [f"MU{i+1}" for i in range(len(top_muni_names))]
    indig_codes = [f"IL{i+1}" for i in range(len(top_indig_names))]
    cons_codes = [f"C{i+1}" for i in range(len(top_cons_names))]

    print("7. Computing totals via dissolve for summaries...")
    if len(indig_gdf) > 0:
        indig_dissolved = indig_gdf.dissolve()
        try:
            indig_clipped = season_yearly_maps.rio.clip(indig_dissolved.geometry, indig_gdf.crs, drop=True)
            indig_yearly = indig_clipped.sum(dim=['lat', 'lon']).load()
            indig_years = indig_yearly.year.values
            indig_values = indig_yearly.values
            indig_current = float(indig_yearly.sel(year=current_year).values)
        except Exception:
            indig_years = last_years
            indig_values = np.zeros(len(last_years))
            indig_current = 0
    else:
        indig_years = last_years
        indig_values = np.zeros(len(last_years))
        indig_current = 0

    if len(cons_gdf) > 0:
        cons_gdf['geometry'] = cons_gdf.geometry.buffer(0)
        cons_dissolved = cons_gdf.dissolve()
        try:
            cons_clipped = season_yearly_maps.rio.clip(cons_dissolved.geometry, cons_gdf.crs, drop=True)
            cons_yearly = cons_clipped.sum(dim=['lat', 'lon']).load()
            cons_years = cons_yearly.year.values
            cons_values = cons_yearly.values
            cons_current = float(cons_yearly.sel(year=current_year).values)
        except Exception:
            cons_years = last_years
            cons_values = np.zeros(len(last_years))
            cons_current = 0
    else:
        cons_years = last_years
        cons_values = np.zeros(len(last_years))
        cons_current = 0

    if top_muni:
        total_top_muni = sum(top_muni_values)
        pct_sa = (total_top_muni / season_year_value) * 100 if season_year_value > 0 else 0
        states = ', '.join(sorted(set(top_muni_states)))
        muni_text = (f"Among the top 5 municipalities, the states of {states} are most affected.\n"
                     f"The total number of fires in these five municipalities alone was {total_top_muni:,.0f},\n"
                     f"representing {pct_sa:.1f}% of all fires in South America.")
    else:
        muni_text = "No fire data for municipalities."

    if top_indig:
        total_top_indig = sum(top_indig_values)
        pct_indig = (indig_current / season_year_value) * 100 if season_year_value > 0 else 0
        indig_text = (f"During {season_name} {year}, fires affected several indigenous lands.\n"
                      f"The five territories with the most fire occurrences are shown.\n"
                      f"A total of {indig_current:,.0f} hotspots were recorded on indigenous lands,\n"
                      f"representing {pct_indig:.1f}% of all fires in South America.")
    else:
        indig_text = "No fire data for indigenous lands."

    if top_cons:
        total_top_cons = sum(top_cons_values)
        pct_cons = (cons_current / season_year_value) * 100 if season_year_value > 0 else 0
        cons_text = (f"In conservation units, the five most affected areas are \nshown. "
                     f"Overall, {cons_current:,.0f} fires occurred \ninside conservation units, "
                     f"which is {pct_cons:.1f}% \nof the South American total.")
    else:
        cons_text = "No fire data for conservation units."

    # ================================================================
    # PAGE 3 — PREPARE BIOME FIRE COMPARISONS AND IUCN ENDEMISM RESULTS
    # ================================================================
    biomes = gpd.read_file(PATHS["biomes"], engine="pyogrio").to_crs("EPSG:4674")

    biome_labels = {}
    biome_colors = {}
    with open(PATHS["biome_colors"], "r", encoding="utf-8") as f:
        for line in f:
            parts = line.strip().split()
            if len(parts) >= 5:
                eco = int(parts[0])
                r, g, b = map(int, parts[1:4])
                label = " ".join(parts[5:])
                biome_labels[eco] = label
                biome_colors[eco] = (r/255, g/255, b/255)

    biomes['label'] = biomes['ECO_ID'].map(biome_labels)

    print("Processing biome fire counts for page 3...")
    biome_year_data = {}
    for idx, row in biomes.iterrows():
        eco_id = row["ECO_ID"]
        geom = [row.geometry]
        label = biome_labels.get(eco_id, str(eco_id))
        print(f"    Processing {label}...")
        try:
            clipped = fires.rio.clip(geom, biomes.crs, drop=True)
            clipped_season = clipped.where(clipped["time.season"] == season, drop=True)
            seasonal_biome = (
                clipped_season
                .groupby(_season_year(clipped_season, season))
                .sum(dim=["time", "lat", "lon"])
            )
            seasonal_biome = seasonal_biome.sel(year=last_years)
            biome_year_data[label] = seasonal_biome.values
        except Exception:
            biome_year_data[label] = np.zeros(len(last_years))

    df_biome = pd.DataFrame(biome_year_data, index=last_years)
    df_long_biome = df_biome.reset_index().melt(id_vars="index", var_name="Biome", value_name="Fires")
    df_long_biome = df_long_biome.rename(columns={"index": "Year"})
    latest_year = last_years[-1]
    top_biomes = df_biome.loc[latest_year].sort_values(ascending=False).head(5).index
    df_long_biome = df_long_biome[df_long_biome["Biome"].isin(top_biomes)]
    df_plot_biome = df_long_biome.pivot(index="Biome", columns="Year", values="Fires")
    df_plot_biome = df_plot_biome.apply(pd.to_numeric, errors="coerce").fillna(0)
    top_biome = df_plot_biome[current_year].idxmax()
    top_biome_value = df_plot_biome[current_year].max()
    if previous_year in df_plot_biome.columns:
        diff_biome = df_plot_biome[current_year] - df_plot_biome[previous_year]
        inc_biome = diff_biome.abs().idxmax()
        biome_inc_value = diff_biome.loc[inc_biome]
    else:
        inc_biome = None
        biome_inc_value = None

    iucn_paths = {name: str(path) for name, path in IUCN_RASTERS.items()}
    fire_counts = map_current.load()

    low_fire, med_fire, high_fire = [], [], []
    groups = list(iucn_paths.keys())

    print("3. Processing IUCN taxonomic classes...")
    for group, path in iucn_paths.items():
        print(f"    Processing {group}...")
        rwr = rioxarray.open_rasterio(path, masked=True).squeeze()
        rwr = rwr.rio.clip([sa_union], crs=countries.crs, drop=True)
        rwr = rwr.rio.reproject_match(fire_counts)
        if 'x' in rwr.dims and 'y' in rwr.dims:
            rwr = rwr.rename({'x': 'lon', 'y': 'lat'})
        rwr, fc = xr.align(rwr, fire_counts, join='exact')
        rwr_vals = rwr.values.flatten()
        fc_vals = fc.values.flatten()
        valid = ~np.isnan(rwr_vals) & ~np.isnan(fc_vals)
        rwr_vals = rwr_vals[valid]
        fc_vals = fc_vals[valid]
        if len(rwr_vals) == 0:
            low_fire.append(0); med_fire.append(0); high_fire.append(0)
            continue
        p60 = np.percentile(rwr_vals, 60)
        p80 = np.percentile(rwr_vals, 80)
        low_mask = rwr_vals <= p60
        med_mask = (rwr_vals > p60) & (rwr_vals <= p80)
        high_mask = rwr_vals > p80
        low_fire.append(np.sum(fc_vals[low_mask]))
        med_fire.append(np.sum(fc_vals[med_mask]))
        high_fire.append(np.sum(fc_vals[high_mask]))

    low_fire = np.nan_to_num(np.array(low_fire))
    med_fire = np.nan_to_num(np.array(med_fire))
    high_fire = np.nan_to_num(np.array(high_fire))

    total_fire = low_fire + med_fire + high_fire
    high_frac = np.divide(high_fire, total_fire, where=total_fire > 0, out=np.zeros_like(high_fire))
    if np.any(total_fire > 0):
        top_iucn_group_high = groups[np.argmax(high_frac)]
        top_high_frac = high_frac[np.argmax(high_frac)] * 100
    else:
        top_iucn_group_high = "none"
        top_high_frac = 0

    # ================================================================
    # PAGES 4–5 — PREPARE CLIMATE AND AEROSOL OPTICAL DEPTH DATA
    # ================================================================
    print("8. Getting data for the climatic analysis...")
    print("    Fetching ET (MOD16A2GF, gap-filled, 2000–present)...")
    et_col = ee.ImageCollection("MODIS/061/MOD16A2GF") \
        .filterDate(start_date, end_date) \
        .filterBounds(region) \
        .select("ET")
    et_img = et_col.sum().multiply(0.1).clip(region)
    ds_et = xr.open_dataset(et_img, engine="ee", scale=0.1, geometry=region, crs="EPSG:4326")
    et = ds_et["ET"].squeeze().transpose("lat", "lon").sortby("lat")
    et = et.rio.write_crs("EPSG:4326")
    et = et.rio.set_spatial_dims(x_dim="lon", y_dim="lat")

    print("    Fetching LST (MOD11A1, 2000–present)...")
    lst_col = ee.ImageCollection("MODIS/061/MOD11A1") \
        .filterDate(start_date, end_date) \
        .filterBounds(region) \
        .select("LST_Day_1km")
    lst_img = lst_col.mean().multiply(0.02).subtract(273.15).clip(region)
    ds_lst = xr.open_dataset(lst_img, engine="ee", scale=0.1, geometry=region, crs="EPSG:4326")
    lst = ds_lst["LST_Day_1km"].squeeze().transpose("lat", "lon").sortby("lat")
    lst = lst.rio.write_crs("EPSG:4326")
    lst = lst.rio.set_spatial_dims(x_dim="lon", y_dim="lat")

    print("    Fetching precipitation (CHIRPS, 1981–present)...")
    chirps_col = ee.ImageCollection("UCSB-CHC/CHIRPS/V3/DAILY_SAT") \
        .filterDate(start_date, end_date) \
        .filterBounds(region) \
        .select("precipitation")
    rain_img = chirps_col.sum().clip(region)
    ds_rain = xr.open_dataset(rain_img, engine="ee", scale=0.05, geometry=region, crs="EPSG:4326")
    rain = ds_rain["precipitation"].squeeze().transpose("lat", "lon").sortby("lat")
    rain = rain.rio.write_crs("EPSG:4326")
    rain = rain.rio.set_spatial_dims(x_dim="lon", y_dim="lat")

    print("Fetching AOD (MCD19A2, 2000–present)...")
    aod_col = ee.ImageCollection("MODIS/061/MCD19A2_GRANULES") \
        .filterDate(start_date, end_date) \
        .filterBounds(region) \
        .select("Optical_Depth_055")
    aod_img = aod_col.max().multiply(0.001).clip(region)
    ds_aod = xr.open_dataset(aod_img, engine="ee", scale=0.1, geometry=region, crs="EPSG:4326")
    aod = ds_aod["Optical_Depth_055"].squeeze().transpose("lat", "lon").sortby("lat")
    aod = aod.rio.write_crs("EPSG:4326")
    aod = aod.rio.set_spatial_dims(x_dim="lon", y_dim="lat")

    et_mean = float(et.mean().values)
    lst_mean = float(lst.mean().values)
    lst_max = float(lst.max().values)
    precip_mean = float(rain.mean().values)

    print("9. Extracting climate means for the top 5 biomes...")
    biome_climate = {}
    for biome in top_biomes:
        print(f"    Processing {biome}...")
        geom_row = biomes[biomes['label'] == biome]
        if geom_row.empty:
            biome_climate[biome] = {
                'ET': et_mean, 'LST': lst_mean, 'Precip': precip_mean,
                'Fires': df_plot_biome.loc[biome, current_year]
            }
            continue
        geom = [geom_row.geometry.iloc[0]]
        try:
            et_mean_biome = float(et.rio.clip(geom, biomes.crs, drop=True).mean().values)
        except:
            et_mean_biome = et_mean
        try:
            lst_mean_biome = float(lst.rio.clip(geom, biomes.crs, drop=True).mean().values)
        except:
            lst_mean_biome = lst_mean
        try:
            rain_mean_biome = float(rain.rio.clip(geom, biomes.crs, drop=True).mean().values)
        except:
            rain_mean_biome = precip_mean
        biome_climate[biome] = {
            'ET': et_mean_biome,
            'LST': lst_mean_biome,
            'Precip': rain_mean_biome,
            'Fires': df_plot_biome.loc[biome, current_year]
        }

    climate_df = pd.DataFrame(biome_climate).T.loc[top_biomes]
    print("Climate DataFrame ready.")

    top_biomes = list(top_biomes)
    valid_top_biomes = []
    for b in top_biomes:
        mask = biomes['label'] == b
        if mask.any() and not biomes.loc[mask, 'geometry'].is_empty.all():
            valid_top_biomes.append(b)
    if not valid_top_biomes:
        raise ValueError("No valid biomes among top 5 fire biomes.")
    top_biomes = valid_top_biomes
    top_fire_biome = top_biomes[0]
    top_fire_count = df_biome.loc[latest_year, top_fire_biome]

    print("3. Extracting AOD statistics for top biomes...")
    biome_aod_stats = {}
    for biome in top_biomes:
        geom_row = biomes[biomes['label'] == biome]
        if geom_row.empty:
            continue
        geom = [geom_row.geometry.unary_union]
        try:
            aod_clip = aod.rio.clip(geom, biomes.crs, drop=True)
            mean_aod = float(aod_clip.mean().values)
            high_frac_biome = ((aod_clip > 0.4).sum().values / aod_clip.count().values) * 100
            biome_aod_stats[biome] = {'mean': mean_aod, 'high_frac': high_frac_biome}
        except Exception as e:
            print(f"AOD clip failed for {biome}: {e}")
            biome_aod_stats[biome] = {'mean': np.nan, 'high_frac': np.nan}

    aod_mean_total = float(aod.mean().values)
    aod_high_frac_total = ((aod > 0.4).sum().values / aod.count().values) * 100

    geom_row = biomes[biomes['label'] == top_fire_biome]
    if not geom_row.empty and not geom_row.geometry.is_empty.all():
        biome_geom = geom_row.geometry.unary_union
        sectors, sector_names = create_directional_sectors(biome_geom)
        sector_fires = {}
        for i, sect_poly in enumerate(sectors):
            try:
                clipped = map_current.rio.clip([sect_poly], biomes.crs, drop=True)
                sector_fires[sector_names[i]] = float(clipped.sum().values)
            except:
                sector_fires[sector_names[i]] = 0
        if sector_fires:
            max_sector = max(sector_fires, key=sector_fires.get)
            max_sector_value = sector_fires[max_sector]
        else:
            max_sector = "unknown"; max_sector_value = 0
    else:
        max_sector = "unknown"; max_sector_value = 0

    aod_qual = "higher than 0.4" if aod_mean_total > 0.4 else "lower than 0.4"

    valid_aod = {b: biome_aod_stats[b]['mean'] for b in top_biomes
                 if not np.isnan(biome_aod_stats[b]['mean'])}
    if valid_aod:
        highest_aod_biome = max(valid_aod, key=valid_aod.get)
        highest_aod_val = valid_aod[highest_aod_biome]
        lowest_aod_biome = min(valid_aod, key=valid_aod.get)
        lowest_aod_val = valid_aod[lowest_aod_biome]
    else:
        highest_aod_biome = lowest_aod_biome = "unknown"
        highest_aod_val = lowest_aod_val = 0

    aod_text = (
        f"In {season_name} {current_year}, the mean Aerosol Optical Depth (AOD) "
        f"over South America was {aod_mean_total:.2f}, which is {aod_qual}. "
        f"Poor air quality conditions (AOD > 0.4) affected "
        f"{aod_high_frac_total:.1f}% of the continent. "
        f"Among the most fire-affected biomes, {highest_aod_biome} recorded "
        f"the highest mean AOD ({highest_aod_val:.2f}), while "
        f"{lowest_aod_biome} had the lowest ({lowest_aod_val:.2f})."
    )
    fire_text = (
        f"Fire activity was most intense in the {top_fire_biome} biome, "
        f"which recorded {top_fire_count:,.0f} hotspots during "
        f"{season_name} {current_year}. "
        f"Within this biome, its {max_sector} sector experienced "
        f"the highest concentration of fires "
        f"({max_sector_value:,.0f} detections)."
    )
    combined_summary = wrap_text(aod_text) + "\n\n" + wrap_text(fire_text)

    # -----------------------------------------------------------------
    # PAGE DRAWERS
    # -----------------------------------------------------------------
    def draw_page_1(fig):
        fig.text(0.05, 0.83, "General scenario and\nterritorial dynamics", fontsize=16, style='italic')
        fig.text(0.05, 0.78,
                 f"This bulletin presents an overview of fire activity in South America "
                 f"for {season_name} {year}.",
                 fontsize=12)

        time_idx = total_sum.indexes["time"].to_datetimeindex()
        values = total_sum.values

        ax = fig.add_axes([0.125, 0.455, 0.55, 0.25])
        ax.plot(time_idx, values, color='#b0b0b0', linewidth=1)
        ax.set(ylabel="Fire counts", xlabel="Year")
        ax.yaxis.set_major_formatter(formatter)
        ax.margins(x=0)
        ax.spines[['top', 'right']].set_visible(False)
        ax.grid(True, alpha=0.3, linestyle='--')

        inset = ax.inset_axes([0.55, 0.8, 0.90, 0.35])

        # -----------------------------------------------------------------
        # PAGE-1 INSET
        #   MAM / JJA / SON  -> Jan-Dec of `year`, season highlighted
        #   DJF              -> Option C: Mar (Y-1) to Feb (Y),
        #                       Dec-Feb (the DJF season) highlighted at the end
        # -----------------------------------------------------------------
        if season == "DJF":
            inset_start = pd.Timestamp(f"{year - 1}-03-01")
            inset_end = pd.Timestamp(f"{year}-03-01")
            djf_start = pd.Timestamp(f"{year - 1}-12-01")
            djf_end = pd.Timestamp(f"{year}-03-01")

            inset_mask = (time_idx >= inset_start) & (time_idx < inset_end)
            inset_time = time_idx[inset_mask]
            inset_values = values[inset_mask]

            inset.plot(inset_time, inset_values, color='#C44E52', linewidth=2)
            inset.set(xlim=(inset_time.min(), inset_time.max()),
                      ylim=(inset_values.min(), inset_values.max()))
            inset.margins(x=0)
            inset.axvspan(djf_start, djf_end, alpha=0.2, color='#C44E52')

            inset.xaxis.set_major_formatter(DateFormatter('%b %Y'))
            inset.xaxis.set_major_locator(MonthLocator())
        else:
            year_mask = time_idx.year == year
            year_time = time_idx[year_mask]
            year_values = values[year_mask]

            inset.plot(year_time, year_values, color='#C44E52', linewidth=2)
            inset.set(xlim=(year_time.min(), year_time.max()),
                      ylim=(year_values.min(), year_values.max()))
            inset.margins(x=0)

            season_months = {'MAM': [3, 4, 5], 'JJA': [6, 7, 8], 'SON': [9, 10, 11]}
            months_to_highlight = season_months.get(season, [])
            for month in months_to_highlight:
                s_dt = pd.Timestamp(f"{year}-{month:02d}-01")
                e_dt = pd.Timestamp(f"{year}-{month + 1:02d}-01") if month < 12 else pd.Timestamp(f"{year + 1}-01-01")
                inset.axvspan(s_dt, e_dt, alpha=0.2, color='#C44E52')

            inset.xaxis.set_major_formatter(DateFormatter('%b'))
            inset.xaxis.set_major_locator(MonthLocator())

        inset.spines[['top', 'right']].set_visible(False)
        for spine in inset.spines.values():
            spine.set_linewidth(0.8)
        plt.setp(inset.xaxis.get_majorticklabels(), rotation=45, ha='right')
        ax.indicate_inset_zoom(inset, edgecolor="#888888", linewidth=1)

        if pct_diff > 0:
            fig.text(0.05, 0.35,
                     f"In {year}, there were more wildfires in {season_name} than \n"
                     f"in other years, with an increase of {pct_diff:.2f}% \n"
                     f"compared with the seasonal mean.", fontsize=12)
        elif pct_diff < 0:
            fig.text(0.05, 0.35,
                     f"In {year}, there were fewer wildfires in {season_name} than \n"
                     f"in other years, with a decrease of {abs(pct_diff):.2f}% \n"
                     f"compared with the seasonal mean.", fontsize=12)
        else:
            fig.text(0.05, 0.35,
                     f"In {year}, wildfire activity was equal to the seasonal mean.",
                     fontsize=12)

        ax_bar = fig.add_axes([0.125, 0.1, 0.75, 0.22])
        countries_plot = df_plot.index
        years_plot = sorted(df_plot.columns)
        x = np.arange(len(countries_plot))
        width = 0.18
        for i, year_i in enumerate(years_plot):
            ax_bar.bar(x + i*width - (width*(len(years_plot)-1)/2),
                       df_plot[year_i].values, width=width, label=str(year_i), color=colors[i])
        ax_bar.set_xticks(x)
        ax_bar.set_xticklabels(countries_plot, rotation=0, ha='right')
        ax_bar.set_xlabel("Country")
        ax_bar.set_ylabel("Fire counts")
        ax_bar.yaxis.set_major_formatter(formatter)
        ax_bar.spines[['top', 'right']].set_visible(False)
        ax_bar.grid(True, alpha=0.3, linestyle='--')
        ax_bar.margins(x=0.01)
        ax_bar.legend(title="Year", loc='upper left', bbox_to_anchor=(1.01, 1), frameon=False, fontsize=8)

        if inc_country is not None:
            change_word = "increase" if inc_value > 0 else ("decrease" if inc_value < 0 else "change")
            fig.text(0.145, 0.25,
                     f"In {current_year}, {top_country} recorded the highest number \nof wildfires ({top_value:,.0f}) "
                     f"compared with the \nother countries. However, the largest {change_word} \ncompared to {previous_year} "
                     f"was also observed in {inc_country}.", fontsize=9)
        else:
            fig.text(0.145, 0.31,
                     f"In {current_year}, {top_country} recorded the highest number \nof wildfires ({top_value:,.0f}) "
                     f"compared with the other countries. \n", fontsize=11)

    def draw_page_2(fig):
        fig.text(0.05, 0.83, "Territory and Conservation", fontsize=16, style='italic')
        fig.text(0.05, 0.78, muni_text, fontsize=12, ha='left', va='top')

        ax_muni = fig.add_axes([0.15, 0.5, 0.7, 0.2])
        y_pos = np.arange(len(top_muni_names))
        ax_muni.barh(y_pos, top_muni_values, color='firebrick', edgecolor='none')
        ax_muni.set_yticks(y_pos)
        ax_muni.set_yticklabels(muni_codes, fontsize=9)
        ax_muni.set_xlabel("Fire counts")
        ax_muni.set_title("Top 5 municipalities", fontsize=10, pad=5)
        ax_muni.xaxis.set_major_formatter(formatter)
        ax_muni.spines[['top', 'right', 'left']].set_visible(False)
        ax_muni.tick_params(left=False)
        ax_muni.grid(True, alpha=0.3, linestyle='--', axis='x')

        fig.text(0.05, 0.45, indig_text, fontsize=9, ha='left', va='top')

        ax_indig = fig.add_axes([0.1, 0.15, 0.35, 0.2])
        y_pos_ind = np.arange(len(top_indig_names))
        ax_indig.barh(y_pos_ind, top_indig_values, color='firebrick', edgecolor='none')
        ax_indig.set_yticks(y_pos_ind)
        ax_indig.set_yticklabels(indig_codes, fontsize=9)
        ax_indig.set_xlabel("Fire counts")
        ax_indig.set_title("Top 5 indigenous lands", fontsize=9)
        ax_indig.xaxis.set_major_formatter(formatter)
        ax_indig.spines[['top', 'right', 'left']].set_visible(False)
        ax_indig.tick_params(left=False)
        ax_indig.grid(True, alpha=0.3, linestyle='--', axis='x')

        fig.text(0.55, 0.45, cons_text, fontsize=9, ha='left', va='top')

        ax_cons = fig.add_axes([0.55, 0.15, 0.35, 0.2])
        y_pos_cons = np.arange(len(top_cons_names))
        ax_cons.barh(y_pos_cons, top_cons_values, color='firebrick', edgecolor='none')
        ax_cons.set_yticks(y_pos_cons)
        ax_cons.set_yticklabels(cons_codes, fontsize=9)
        ax_cons.set_xlabel("Fire counts")
        ax_cons.set_title("Top 5 conservation units", fontsize=9)
        ax_cons.xaxis.set_major_formatter(formatter)
        ax_cons.spines[['top', 'right', 'left']].set_visible(False)
        ax_cons.tick_params(left=False)
        ax_cons.grid(True, alpha=0.3, linestyle='--', axis='x')

        fig.text(0.05, 0.075, "*See page 6 for the full names.", fontsize=9, ha='left', va='top')

    def draw_page_3(fig):
        fig.text(0.05, 0.83, "Biogeography", fontsize=16, style='italic')
        if inc_biome is not None:
            change_word_biome = "increase" if biome_inc_value > 0 else ("decrease" if biome_inc_value < 0 else "change")
        else:
            change_word_biome = ""

        fig.text(0.05, 0.75,
                 f"In {current_year}, {top_biome} recorded the highest number "
                 f"of wildfires ({top_biome_value:,.0f}) among all South American\nbiomes.",
                 fontsize=12)

        ax_bar = fig.add_axes([0.105, 0.45, 0.75, 0.25])
        years_biome_plot = sorted(df_plot_biome.columns)
        x = np.arange(len(df_plot_biome.index))
        width = 0.18
        for i, y in enumerate(years_biome_plot):
            ax_bar.bar(x + i*width - (width*(len(years_biome_plot)-1)/2),
                       df_plot_biome[y].values, width=width, label=str(y), color=colors[i])
        ax_bar.set_xticks(x)
        ax_bar.set_xticklabels(df_plot_biome.index, rotation=0, ha='right')
        ax_bar.set_ylabel("Fire counts")
        ax_bar.yaxis.set_major_formatter(formatter)
        ax_bar.spines[['top', 'right']].set_visible(False)
        ax_bar.grid(True, alpha=0.3, linestyle='--')
        ax_bar.legend(title="Year", loc='upper left', bbox_to_anchor=(1.01, 1), frameon=False, fontsize=8)

        if inc_biome is not None:
            fig.text(0.05, 0.39,
                     f"The largest {change_word_biome} compared to {previous_year} "
                     f"was observed in\n{inc_biome}.",
                     fontsize=10)

        ax_iucn = fig.add_axes([0.105, 0.1, 0.5, 0.22])
        x_pos = np.arange(len(groups))
        width = 0.6
        ax_iucn.bar(x_pos, low_fire, width, label='Low RWR (≤60th pct)', color='#e8cd1e', edgecolor='none')
        ax_iucn.bar(x_pos, med_fire, width, bottom=low_fire,
                    label='Medium RWR (60-80th pct)', color='#e8971e', edgecolor='none')
        ax_iucn.bar(x_pos, high_fire, width, bottom=low_fire+med_fire,
                    label='High RWR (>80th pct)', color='#b84721', edgecolor='none')
        ax_iucn.set_ylabel("Fire counts")
        ax_iucn.yaxis.set_major_formatter(formatter)
        ax_iucn.set_xticks(x_pos)
        ax_iucn.set_xticklabels(groups, fontsize=8)
        ax_iucn.set_title("Fire activity by endemism level", fontsize=10)
        ax_iucn.spines[['top', 'right']].set_visible(False)
        ax_iucn.grid(True, alpha=0.3, linestyle='--', axis='y')
        ax_iucn.legend(loc='upper left', bbox_to_anchor=(1.02, 1), frameon=False, fontsize=8)

        fig.text(0.62, 0.18,
                 f"Fires in high endemism areas\n"
                 f"(>80th percentile) are most\n"
                 f"pronounced for {top_iucn_group_high},\n"
                 f"where {top_high_frac:.1f}% of fires\n"
                 f"occurred in those pixels.",
                 fontsize=10)

    def draw_page_4(fig):
        fig.text(0.05, 0.85, "Diagnosis", fontsize=16, style='italic', weight='bold')
        intensity = "high" if lst_max > 35 else ("medium" if lst_max > 25 else "low")
        fig.text(0.05, 0.800,
                 f"{season_name.capitalize()} {year} had an average ET of {et_mean:.1f} mm, while the precipitation average was {precip_mean:.1f} mm.\n"
                 f"LST reached {intensity} levels, averaging {lst_mean:.1f}°C and peaking at {lst_max:.1f}°C.",
                 fontsize=12)

        map_y_start = 0.52
        map_height = 0.25

        ax_et = fig.add_axes([0.1, map_y_start, 0.22, map_height], projection=PC_PROJ)
        ax_et.set_extent(extent, crs=PC_PROJ)
        ax_et.add_feature(cfeature.LAND.with_scale("50m"), facecolor="#d9d9d9", edgecolor="none")
        cf_et = ax_et.contourf(et.lon, et.lat, et, levels=20, cmap=et_cmap,
                               extend="max", transform=PC_PROJ, vmin=0, vmax=300)
        ax_et.coastlines(resolution="50m", linewidth=0.7)
        ax_et.set_title("ET (mm)", fontsize=8, pad=2)
        ax_et.set_xticks([]); ax_et.set_yticks([])
        cbar_ax = fig.add_axes([0.1, map_y_start-0.025, 0.22, 0.01])
        fig.colorbar(cf_et, cax=cbar_ax, orientation='horizontal').ax.tick_params(labelsize=6)

        ax_lst = fig.add_axes([0.38, map_y_start, 0.22, map_height], projection=PC_PROJ)
        ax_lst.set_extent(extent, crs=PC_PROJ)
        ax_lst.add_feature(cfeature.LAND.with_scale("50m"), facecolor="#d9d9d9", edgecolor="none")
        cf_lst = ax_lst.contourf(lst.lon, lst.lat, lst, levels=20, cmap="coolwarm",
                                 extend="both", transform=PC_PROJ, vmin=0, vmax=35)
        ax_lst.coastlines(resolution="50m", linewidth=0.7)
        ax_lst.set_title("LST (°C)", fontsize=8, pad=2)
        ax_lst.set_xticks([]); ax_lst.set_yticks([])
        cbar_ax2 = fig.add_axes([0.38, map_y_start-0.025, 0.22, 0.01])
        fig.colorbar(cf_lst, cax=cbar_ax2, orientation='horizontal').ax.tick_params(labelsize=6)

        ax_rain = fig.add_axes([0.66, map_y_start, 0.22, map_height], projection=PC_PROJ)
        ax_rain.set_extent(extent, crs=PC_PROJ)
        ax_rain.add_feature(cfeature.LAND.with_scale("50m"), facecolor="#d9d9d9", edgecolor="none")
        cf_rain = ax_rain.contourf(rain.lon, rain.lat, rain, levels=20, cmap=rain_cmap,
                                   extend="max", transform=PC_PROJ, vmin=0, vmax=3000)
        ax_rain.coastlines(resolution="50m", linewidth=0.7)
        ax_rain.set_title("Precip (mm)", fontsize=8, pad=2)
        ax_rain.set_xticks([]); ax_rain.set_yticks([])
        cbar_ax3 = fig.add_axes([0.66, map_y_start-0.025, 0.22, 0.01])
        fig.colorbar(cf_rain, cax=cbar_ax3, orientation='horizontal').ax.tick_params(labelsize=6)

        ax_chart = fig.add_axes([0.1, 0.12, 0.5, 0.28])
        if climate_df.empty or climate_df.isnull().all().all():
            ax_chart.text(0.5, 0.5, 'Climate data not available for selected biomes',
                          transform=ax_chart.transAxes, ha='center', va='center', fontsize=10)
        else:
            x = np.arange(len(climate_df.index))
            width = 0.25
            et_vals = climate_df['ET'].fillna(et_mean).values
            precip_vals = climate_df['Precip'].fillna(precip_mean).values
            ax_chart.bar(x - width/2, et_vals, width, label='Evapotranspiration (mm)', color='#42b89c', alpha=0.8)
            ax_chart.bar(x + width/2, precip_vals, width, label='Precipitation (mm)', color='#0d47a1', alpha=0.8)
            ax_chart.set_ylabel('ET / Precip (mm)', fontsize=8, color='#0d47a1')
            ax_chart.tick_params(axis='y', labelcolor='#0d47a1')
            ax_chart.yaxis.set_major_formatter(formatter)

            ax_lst2 = ax_chart.twinx()
            lst_vals = climate_df['LST'].fillna(lst_mean).values
            ax_lst2.plot(x, lst_vals, color='firebrick', marker='o', linestyle='-',
                         linewidth=2, markersize=6, label='Land Surf. Temp. (°C)')
            ax_lst2.set_ylabel('LST (°C)', fontsize=8, color='firebrick')
            ax_lst2.tick_params(axis='y', labelcolor='firebrick')

            ax_chart.set_xticks(x)
            ax_chart.set_xticklabels(wrap_labels(climate_df.index, width=12), fontsize=7, ha='center')
            ax_chart.set_xlabel('Biome', fontsize=8)
            ax_chart.set_title('Climate conditions in the most fire-affected biomes', fontsize=9, pad=10)

            lines1, labels1 = ax_chart.get_legend_handles_labels()
            lines2, labels2 = ax_lst2.get_legend_handles_labels()
            ax_chart.legend(lines1 + lines2, labels1 + labels2, loc='upper right',
                            fontsize=6, frameon=False)
            ax_chart.grid(True, alpha=0.3, linestyle='--', axis='y')
            ax_chart.spines[['top']].set_visible(False)

            top_biome_climate = climate_df.index[0]
            top_fires = climate_df.iloc[0]['Fires'] if 'Fires' in climate_df.columns else 0
            fig.text(0.05, 0.45,
                     f"The {top_biome_climate} biome recorded the highest fire count ({top_fires:,.0f}) in {season_name} {year}, "
                     f"with relatively high LST and low precipitation.",
                     fontsize=9)

            fig.text(0.70, 0.275,
                     "*Normally, the combination of \nhigher temperatures, low \nprecipitation and high ET \nfavors the occurrence \nof wildfires.\n"
                     "Also, an ET higher than\nprecipitation implies a\nwater deficit that can\naccumulate over time.",
                     fontsize=9)

    def draw_page_5(fig):
        map_y_start = 0.45
        map_height = 0.4
        map_width = 0.375

        ax_aod = fig.add_axes([0.1, map_y_start, map_width, map_height], projection=PC_PROJ)
        ax_aod.set_extent(extent, crs=PC_PROJ)
        ax_aod.add_feature(cfeature.LAND.with_scale("50m"), facecolor="#d9d9d9", edgecolor="none")
        levels_aod = np.linspace(0, 1.2, 25)
        cf_aod = ax_aod.contourf(aod.lon, aod.lat, aod, levels=levels_aod,
                                 cmap=smoke_cmap, extend="max", transform=PC_PROJ, vmax=1)
        ax_aod.coastlines(resolution="50m", linewidth=0.9)
        ax_aod.set_title("Aerosol Optical Depth (550 nm)", fontsize=10, pad=2)
        ax_aod.set_xticks([]); ax_aod.set_yticks([])
        cbar_ax_aod = fig.add_axes([0.1, map_y_start - 0.02, map_width, 0.01])
        fig.colorbar(cf_aod, cax=cbar_ax_aod, orientation='horizontal').ax.tick_params(labelsize=7)

        ax_fire = fig.add_axes([0.5, map_y_start, map_width, map_height], projection=PC_PROJ)
        ax_fire.set_extent(extent, crs=PC_PROJ)
        ax_fire.add_feature(cfeature.LAND.with_scale("50m"), facecolor="#e3e3e3", edgecolor="none", zorder=0)
        im = ax_fire.pcolormesh(fire_current_align.lon, fire_current_align.lat, fire_current_align,
                                cmap="autumn_r", vmin=0, vmax=30, transform=PC_PROJ, rasterized=True)
        ax_fire.coastlines(resolution="50m", linewidth=0.9)
        ax_fire.set_title("Fire Counts (seasonal sum)", fontsize=10, pad=2)
        ax_fire.set_xticks([]); ax_fire.set_yticks([])
        cbar_ax_fire = fig.add_axes([0.5, map_y_start - 0.02, map_width, 0.01])
        fig.colorbar(im, cax=cbar_ax_fire, orientation='horizontal', extend="max").ax.tick_params(labelsize=7)

        chart_y_start = 0.12
        chart_height = 0.25
        chart_width = 0.375
        text_width = 0.45

        ax_chart = fig.add_axes([0.1, chart_y_start, chart_width, chart_height])
        plot_biomes = [b for b in top_biomes if b in biome_aod_stats and not np.isnan(biome_aod_stats[b]['mean'])]
        if not plot_biomes:
            plot_biomes = top_biomes
            mean_aod_vals = [0.5] * len(plot_biomes)
            high_frac_vals = [20] * len(plot_biomes)
        else:
            mean_aod_vals = [biome_aod_stats[b]['mean'] for b in plot_biomes]
            high_frac_vals = [biome_aod_stats[b]['high_frac'] for b in plot_biomes]

        x = np.arange(len(plot_biomes))
        width = 0.6
        ax_chart.bar(x, mean_aod_vals, width, color='#f57c00', alpha=0.8, label='Mean AOD')
        ax_chart.set_ylabel('Mean AOD', fontsize=9, color='#f57c00')
        ax_chart.tick_params(axis='y', labelcolor='#f57c00')
        ax_chart.set_ylim(0, max(mean_aod_vals) * 1.2 if mean_aod_vals else 1)

        ax_chart2 = ax_chart.twinx()
        ax_chart2.plot(x, high_frac_vals, color='#0d47a1', marker='o',
                       linestyle='-', linewidth=2, markersize=5, label='% > 0.4')
        ax_chart2.set_ylabel('% pixels AOD > 0.4', fontsize=9, color='#0d47a1')
        ax_chart2.tick_params(axis='y', labelcolor='#0d47a1')
        ax_chart2.set_ylim(0, 100)

        ax_chart.set_xticks(x)
        ax_chart.set_xticklabels([textwrap.fill(b, 10) for b in plot_biomes], ha='center', fontsize=8)
        ax_chart.set_xlabel('Biome', fontsize=9)
        ax_chart.set_title('AOD in the most fire-affected biomes', fontsize=10)

        lines1, labels1 = ax_chart.get_legend_handles_labels()
        lines2, labels2 = ax_chart2.get_legend_handles_labels()
        ax_chart.legend(lines1 + lines2, labels1 + labels2, loc='upper right', fontsize=7, frameon=False)
        ax_chart.grid(True, alpha=0.3, linestyle='--', axis='y')

        ax_text = fig.add_axes([0.55, 0.14, text_width, chart_height])
        ax_text.axis('off')
        ax_text.text(0, 1, combined_summary, transform=ax_text.transAxes,
                     fontsize=9, va='top', ha='left', linespacing=1.5)

    def draw_page_6(fig):
        fig.text(0.05, 0.85, "Summary", fontsize=16, style='italic')

        y_start = 0.78
        line_space = 0.035

        fig.text(0.05, y_start, "Top Municipalities", fontsize=13, weight='bold')
        y = y_start - 0.04
        for code, name, value in zip(muni_codes, top_muni_names, top_muni_values):
            fig.text(0.05, y, f"{code} – {name} ({value:,.0f} fires)", fontsize=10)
            y -= line_space

        y -= 0.03
        fig.text(0.05, y, "Top Indigenous Lands", fontsize=13, weight='bold')
        y -= 0.04
        for code, name, value in zip(indig_codes, top_indig_names, top_indig_values):
            fig.text(0.05, y, f"{code} – {name} ({value:,.0f} fires)", fontsize=10)
            y -= line_space

        y -= 0.03
        fig.text(0.05, y, "Top Conservation Units", fontsize=13, weight='bold')
        y -= 0.04
        for code, name, value in zip(cons_codes, top_cons_names, top_cons_values):
            fig.text(0.05, y, f"{code} – {name} ({value:,.0f} fires)", fontsize=10)
            y -= line_space

    def draw_page_7(fig):
        img = plt.imread(str(PATHS["reference_maps"]))
        img_h, img_w = img.shape[:2]
        max_width = 0.9
        max_height = 0.85
        aspect_img = img_w / img_h
        aspect_page = (W_px * max_width) / (H_px * max_height)
        if aspect_img > aspect_page:
            width = max_width
            height = width / aspect_img * (W_px / H_px)
        else:
            height = max_height
            width = height * aspect_img * (H_px / W_px)
        left = (1 - width) / 2
        bottom = (1 - height) / 2
        ax_img = fig.add_axes([left, bottom, width, height])
        ax_img.imshow(img)
        ax_img.axis("off")

    print("Generating the PDF...")
    page_titles = {
        1: "Political overview", 2: "Political overview",
        3: "Natural overview", 4: "Climate",
        5: "Impacts", 6: "Acronyms", 7: "Reference maps",
    }
    page_drawers = {
        1: draw_page_1, 2: draw_page_2, 3: draw_page_3, 4: draw_page_4,
        5: draw_page_5, 6: draw_page_6, 7: draw_page_7,
    }

    with PdfPages(output_pdf) as pdf:
        for page in range(1, pages + 1):
            fig = plt.figure(figsize=figsize, dpi=dpi)
            draw_header(fig, page_titles.get(page, ""), report_authors)
            draw_function = page_drawers.get(page)
            if draw_function is not None:
                draw_function(fig)
            fig.add_artist(plt.Line2D([0.05, 0.95], [0.05, 0.05], linewidth=1, color="#878787"))
            fig.text(0.05, 0.02, source_info, fontsize=9)
            fig.text(0.95, 0.02, f"Page {page}", ha="right", fontsize=10)
            pdf.savefig(fig)
            plt.close(fig)
        info = pdf.infodict()
        info["Title"] = report_title
        info["Author"] = report_authors

    writer = PdfWriter()
    header_reader = PdfReader(str(PATHS["header_pdf"]))
    for i, _cover_page in enumerate(header_reader.pages, start=1):
        images = convert_from_path(str(PATHS["header_pdf"]), dpi=dpi, first_page=i, last_page=i)
        img = np.array(images[0])
        img_h, img_w = img.shape[:2]
        if img_h > H_px:
            img = img[0:H_px, :, :]
        fig = plt.figure(figsize=figsize, dpi=dpi)
        ax = fig.add_axes([0, 0, 1, 1])
        ax.axis("off")
        ax.imshow(img)
        plt.savefig(temp_cover_page_pdf, dpi=dpi)
        plt.close(fig)
        temp_reader = PdfReader(str(temp_cover_page_pdf))
        writer.add_page(temp_reader.pages[0])
        os.remove(temp_cover_page_pdf)

    dynamic_reader = PdfReader(str(output_pdf))
    for page in dynamic_reader.pages:
        writer.add_page(page)
    with open(final_output_pdf, "wb") as output_file:
        writer.write(output_file)
    print(f"PDF generated successfully: {final_output_pdf}")


# =============================================================================
# BATCH ENTRY POINT
# =============================================================================
def main():
    """Authenticate once, then generate every (year, season) report in order."""
    ee.Authenticate()
    ee.Initialize(project=EE_PROJECT)

    total = len(REPORT_CONFIGS)
    successes = []
    failures = []
    batch_start = time.time()

    print(f"Batch: {total} reports ({len(BATCH_YEARS)} years × {len(BATCH_SEASONS)} seasons)")
    print(f"Years:  {BATCH_YEARS[0]}–{BATCH_YEARS[-1]}")
    print(f"Seasons: {', '.join(BATCH_SEASONS)}")
    print()

    for i, (year, season) in enumerate(REPORT_CONFIGS, 1):
        print()
        print("=" * 72)
        print(f"  REPORT {i}/{total}  —  {season} {year}  ({total - i} remaining)")
        print("=" * 72)
        t0 = time.time()
        try:
            generate_report(year, season)
            elapsed = time.time() - t0
            print(f"\n[{i}/{total}] OK: {season} {year}  ({elapsed/60:.1f} min)\n")
            successes.append((year, season))
        except Exception as e:
            elapsed = time.time() - t0
            print(f"\n[{i}/{total}] FAILED: {season} {year}  ({elapsed/60:.1f} min)")
            print(f"    {type(e).__name__}: {e}\n")
            failures.append((year, season, e))

    batch_elapsed = time.time() - batch_start
    print()
    print("=" * 72)
    print("  BATCH COMPLETE")
    print("=" * 72)
    print(f"  Total elapsed: {batch_elapsed/60:.1f} min  ({batch_elapsed/3600:.1f} h)")
    print(f"  Successful:    {len(successes)}/{total}")
    if successes:
        for y, s in successes:
            print(f"    ✓ {s} {y}")
    if failures:
        print(f"  Failed:        {len(failures)}/{total}")
        for y, s, e in failures:
            print(f"    ✗ {s} {y}  —  {type(e).__name__}: {e}")


if __name__ == "__main__":
    main()
