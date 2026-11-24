
# Seasonal Wildfire Bulletins — WEC
This repository contains the Python code used to automatically generate seasonal wildfire monitoring bulletins for the **World Environmental Conservancy (WEC)**.
The bulletins provide political, environmental, and climatic context for wildfire activity across South America.
The workflow consists of two main steps: preprocessing the original MODIS fire data and generating the final PDF reports.
The fixed report header (REPORT_HEADER.pdf) was created using Canva, while the reference maps (REF_MAPS.png) were prepared in QGIS.

# Guide
## 1. Data acquisition and preprocessing
### 1.1. Downloading fire data
To run the preprocessing workflow, you must first obtain fire data from the MODIS MOD14A1 product. The data can be downloaded through [NASA AppEEARS](https://appeears.earthdatacloud.nasa.gov/), which requires a NASA Earthdata account. Download the data in **NetCDF format**. 

The original data have a spatial resolution of approximately 1 km. To facilitate subsequent processing, we provide a script that resamples the files to a regular 0.1° × 0.1° grid.

### 1.2. Resampling the data
A reference grid is provided [here](./QMD_PROCESSAMENTO/REF_GRID.nc).
The main preprocessing script is: [QMD_PROCESSAMENTO/qmd.py](./QMD_PROCESSAMENTO/qmd.py). Please change the path name for the directory where your MOD14A1 is. 
Run this script to resample the original MOD14A1 files to the reference grid. The processing generates one NetCDF file per year, containing monthly data, which are saved in QMD_PROCESSAMENTO/.
The resulting files are used by the report generation script.

## 2. Report data and directory structure
The datasets and institutional files required to generate the bulletins are available [here (Google Drive)](https://drive.google.com/file/d/1Rh0wuVO9Qqo_N3YZX_CGbW2OucnLloUD/view?usp=drive_link).
The downloaded directory already follows the expected structure. Place it inside the repository as follows:

    REPORT/
        DATA/
            ...

The directory must contain the geographic boundaries, biome information, IUCN rasters, fonts, report header, and reference maps required by the script. The provided directory already has these files, but it is good to verify.
The fire NetCDF files generated during preprocessing must also be available in the directory QMD_PROCESSAMENTO/. 

**Note:** The datasets and institutional reference files are not included in this repository and must be obtained separately. More information about datasets or products are at the last page of the file REPORT_HEADER.pdf. 

## 3. Environment setup
### 3.1. Python environment
Create and activate a Python virtual environment:
python -m venv .venv

On Windows:
.venv\Scripts\activate

Install the required Python packages:
python -m pip install -r requirements_boletim.txt

### 3.2. PDF rendering dependency
The pdf2image package requires Poppler utilities (pdftoppm) to convert PDF pages into images.
Make sure that pdftoppm is available through the system PATH. It may be provided by an existing software distribution, such as MiKTeX on Windows; a separate Poppler installation is not necessarily required.
You can check whether the executable is available by running this [code](./REPORT/verify.py).
If the command returns a valid executable path, the dependency is available. If not, follow the instructions below. 
**3.3. Poppler installation**
- **Windows:** Download a Windows build of Poppler, extract the files, and add the `Library/bin` (or the corresponding `bin` directory in your distribution) to the system PATH. 
- **Linux:** Install Poppler using your distribution's package manager. For example, on Ubuntu/Debian:
  sudo apt install poppler-utils
- **macOS:** Install Poppler using Homebrew:
  brew install poppler

The script uses `pdftoppm` to rasterize the institutional report header before assembling the final PDF.

### **3.3. Google Earth Engine configuration**
A Google Earth Engine account and an associated project are required to generate the bulletins.
In [bulletin_auto.py](./REPORT/bulletin_auto.py), go to the line 79, locate the `USER CONFIGURATION` section and edit:
EE_PROJECT = "your-ee-project"
Replace the example with your Google Earth Engine project ID (usually in the format `ee-xxx`).
The script automatically requests Earth Engine authentication when executed for the first time. Complete the authentication process when prompted.
The project is initialized automatically after authentication.

## **4. Report configuration**
The main configuration parameters are:

| Parameter                                                      | Description                                                                                                                                            |
| -------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------ |
| PATHS                                                          | Paths to the fire data, geographic boundaries, biomes, municipalities, Indigenous Lands, Conservation Units, report header, reference maps, and fonts. |
| IUCN_RASTERS                                                   | Paths to the four weighted species-richness rasters (amphibians, birds, mammals, and reptiles).                                                        |
| EE_PROJECT                                                     | Google Earth Engine project ID.                                                                                                                        |
| REPORT_TITLE                                                   | Main report title.                                                                                                                                     |
| SOURCE_INFO                                                    | Source information displayed in the report footer.                                                                                                     |
| BATCH_YEARS                                                    | List of years for which bulletins will be generated.                                                                                                   |
| BATCH_SEASONS                                                  | List of seasons to include in the batch.                                                                                                               |
| REPORT_CONFIGS                                                 | List of year-season combinations to generate.                                                                                                          |
| PAGE_WIDTH_PX, PAGE_HEIGHT_PX, DPI                             | Report dimensions and resolution.                                                                                                                      |
| NUMBER_OF_PAGES                                                | Number of analytical pages in the report.                                                                                                              |
| MAP_EXTENT, GEE_REGION_BOUNDS                                  | Geographic limits used for maps and Google Earth Engine data acquisition.                                                                              |
| COUNTRY_BAR_COLORS, ET_COLORS, RAIN_COLORS, SMOKE_COLORS       | Color palettes used in the report graphics.                                                                                                            |

**Important:** Changes to PAGE_WIDTH_PX, PAGE_HEIGHT_PX, and DPI may affect the page composition and are therefore not recommended unless necessary. Some of the other parameters (e.g. Title, chosen seasons, colors) can be changed without big problems.

### **4.1. Selecting the reporting period**
The script generates one PDF for each (year, season) combination defined in REPORT_CONFIGS.
The reporting seasons follow the meteorological convention:

| Season | Months                       |
| ------ | ---------------------------- |
| DJF    | December, January, February  |
| MAM    | March, April, May            |
| JJA    | June, July, August           |
| SON    | September, October, November |

To configure the batch, edit `BATCH_YEARS` and `BATCH_SEASONS`.

For example:

```
BATCH_YEARS = list(range(2004, 2026))
BATCH_SEASONS = ["MAM", "JJA", "SON", "DJF"]
REPORT_CONFIGS = [(y, s) for y in BATCH_YEARS for s in BATCH_SEASONS]
```

## **5. Generating the reports**
After completing the installation and configuration steps, execute:

```
python boletim_queimadas_portatil.py
```

The script authenticates with Google Earth Engine, initializes the configured project, and processes all year-season combinations in `REPORT_CONFIGS`.
For each report, the script generates an intermediate analytical PDF:

`miolo_temp.pdf`

It then combines the analytical pages with the institutional report header and saves the final document as ```report_{year}_{season}.pdf```.
For example:

```
report_2025_DJF.pdf
report_2025_MAM.pdf
report_2025_JJA.pdf
report_2025_SON.pdf
```

The final PDFs are saved in the same directory as the Python script.
The batch execution also displays the processing status of each report, including successful executions, failures, and elapsed time.
**Note:** Processing may occasionally fail, so some reports may need to be rerun.

## **6. Code organization**
The main script is organized into the following sections:

| Section                        | Description                                                                                                                          |
| ------------------------------ | ------------------------------------------------------------------------------------------------------------------------------------ |
| USER CONFIGURATION             | File paths, reporting period, Earth Engine project, report dimensions, and color palettes.                                           |
| BATCH CONFIGURATION            | Years, seasons, and year-season combinations to be generated.                                                                        |
| SHARED HELPERS                 | Functions for fonts, report headers, seasonal dates, text wrapping, directional sectors, and fire counts within geographic polygons. |
| generate_report()              | Main function responsible for generating a bulletin for a given year and season.                                                     |
| PAGE 1 DATA                    | Fire time series, seasonal totals, country comparisons, and spatial fire distributions.                                              |
| PAGE 2 DATA                    | Fire counts in municipalities, Indigenous Lands, and Conservation Units.                                                             |
| PAGE 3 DATA                    | Fire distribution across biomes and IUCN weighted species-richness classes.                                                          |
| PAGES 4–5 DATA                 | Evapotranspiration, land surface temperature, precipitation, and aerosol optical depth (AOD).                                        |
| draw_page_1 to draw_page_7     | Functions responsible for the graphical composition of each analytical page.                                                         |
| PAGE GENERATION / FINAL PDF    | Analytical PDF generation, cover rasterization, and assembly of the final document.                                                  |
| main()                         | Earth Engine authentication, project initialization, batch execution, and processing summary.                                        |

### **6.1. Report page structure**
The generated bulletin contains seven analytical pages:

| Page | Content                                                                                                |
| ---- | ------------------------------------------------------------------------------------------------------ |
| 1    | Political overview: fire activity over time and comparisons between South American countries.          |
| 2    | Territorial dynamics: municipalities, Indigenous Lands, and Conservation Units most affected by fires. |
| 3    | Natural overview: fire distribution across biomes and IUCN weighted species-richness classes.          |
| 4    | Climate: evapotranspiration, land surface temperature, and precipitation.                              |
| 5    | Impacts: aerosol optical depth and seasonal fire distribution.                                         |
| 6    | Summary: full names and fire counts for the geographic areas highlighted on page 2.                    |
| 7    | Reference maps.                                                                                        |
