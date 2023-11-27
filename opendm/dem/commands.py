import os
import sys
import rasterio
import numpy
import math
import time
import shutil
import functools
import threading
import glob
import re
from joblib import delayed, Parallel
from opendm.system import run
from opendm import point_cloud
from opendm import io
from opendm import system
from opendm.concurrency import get_max_memory, parallel_map, get_total_memory
from scipy import ndimage
from datetime import datetime
from opendm.vendor.gdal_fillnodata import main as gdal_fillnodata
from opendm import log
import threading

from .ground_rectification.rectify import run_rectification
from . import pdal

try:
    # GDAL >= 3.3
    from osgeo_utils.gdal_proximity import main as gdal_proximity
except ModuleNotFoundError:
    # GDAL <= 3.2
    try:
        from osgeo.utils.gdal_proximity import main as gdal_proximity
    except:
        pass

def classify(lasFile, scalar, slope, threshold, window):
    start = datetime.now()

    try:
        pdal.run_pdaltranslate_smrf(lasFile, lasFile, scalar, slope, threshold, window)
    except:
        log.ODM_WARNING("Error creating classified file %s" % lasFile)

    log.ODM_INFO('Created %s in %s' % (lasFile, datetime.now() - start))
    return lasFile

def rectify(lasFile, reclassify_threshold=5, min_area=750, min_points=500):
    start = datetime.now()

    try:

        log.ODM_INFO("Rectifying {} using with [reclassify threshold: {}, min area: {}, min points: {}]".format(lasFile, reclassify_threshold, min_area, min_points))
        run_rectification(
            input=lasFile, output=lasFile, \
            reclassify_plan='median', reclassify_threshold=reclassify_threshold, \
            extend_plan='surrounding', extend_grid_distance=5, \
            min_area=min_area, min_points=min_points)

        log.ODM_INFO('Created %s in %s' % (lasFile, datetime.now() - start))
    except Exception as e:
        log.ODM_WARNING("Error rectifying ground in file %s: %s" % (lasFile, str(e)))

    return lasFile

error = None

def create_dem(input_point_cloud, dem_type, output_type='max', radiuses=['0.56'], gapfill=True,
                outdir='', resolution=0.1, max_workers=1, max_tile_size=4096,
                decimation=None, keep_unfilled_copy=False,
                apply_smoothing=True, max_tiles=None):
    """ Create DEM from multiple radii, and optionally gapfill """
    
    start = datetime.now()
    kwargs = {
        'input': input_point_cloud,
        'outdir': outdir,
        'outputType': output_type,
        'radiuses': ",".join(map(str, radiuses)),
        'resolution': resolution,
        'maxTiles': 0 if max_tiles is None else max_tiles,
        'decimation': 1 if decimation is None else decimation,
        'classification': 2 if dem_type == 'dtm' else -1
    }
    system.run('renderdem "{input}" '
                '--outdir "{outdir}" '
                '--output-type {outputType} '
                '--radiuses {radiuses} '
                '--resolution {resolution} '
                '--max-tiles {maxTiles} '
                '--decimation {decimation} '
                '--classification {classification} '
                '--force '.format(**kwargs), env_vars={'OMP_NUM_THREADS': max_workers})

    output_file = "%s.tif" % dem_type
    output_path = os.path.abspath(os.path.join(outdir, output_file))

    # Fetch tiles
    tiles = []
    for p in glob.glob(os.path.join(os.path.abspath(outdir), "*.tif")):
        filename = os.path.basename(p)
        m = re.match("^r([\d\.]+)_x\d+_y\d+\.tif", filename)
        if m is not None:
            tiles.append({'filename': p, 'radius': float(m.group(1))})

    if len(tiles) == 0:
        raise system.ExitException("No DEM tiles were generated, something went wrong")

    log.ODM_INFO("Generated %s tiles" % len(tiles))

    # Sort tiles by decreasing radius
    tiles.sort(key=lambda t: float(t['radius']), reverse=True)

    # Create virtual raster
    tiles_vrt_path = os.path.abspath(os.path.join(outdir, "tiles.vrt"))
    tiles_file_list = os.path.abspath(os.path.join(outdir, "tiles_list.txt"))
    with open(tiles_file_list, 'w') as f:
        for t in tiles:
            f.write(t['filename'] + '\n')

    run('gdalbuildvrt -input_file_list "%s" "%s" ' % (tiles_file_list, tiles_vrt_path))

    merged_vrt_path = os.path.abspath(os.path.join(outdir, "merged.vrt"))
    geotiff_tmp_path = os.path.abspath(os.path.join(outdir, 'tiles.tmp.tif'))
    geotiff_small_path = os.path.abspath(os.path.join(outdir, 'tiles.small.tif'))
    geotiff_small_filled_path = os.path.abspath(os.path.join(outdir, 'tiles.small_filled.tif'))
    geotiff_path = os.path.abspath(os.path.join(outdir, 'tiles.tif'))

    # Build GeoTIFF
    kwargs = {
        'max_memory': get_max_memory(),
        'threads': max_workers if max_workers else 'ALL_CPUS',
        'tiles_vrt': tiles_vrt_path,
        'merged_vrt': merged_vrt_path,
        'geotiff': geotiff_path,
        'geotiff_tmp': geotiff_tmp_path,
        'geotiff_small': geotiff_small_path,
        'geotiff_small_filled': geotiff_small_filled_path
    }

    if gapfill:
        # Sometimes, for some reason gdal_fillnodata.py
        # behaves strangely when reading data directly from a .VRT
        # so we need to convert to GeoTIFF first.
        run('gdal_translate '
                '-co NUM_THREADS={threads} '
                '-co BIGTIFF=IF_SAFER '
                '--config GDAL_CACHEMAX {max_memory}% '
                '"{tiles_vrt}" "{geotiff_tmp}"'.format(**kwargs))

        # Scale to 10% size
        run('gdal_translate '
            '-co NUM_THREADS={threads} '
            '-co BIGTIFF=IF_SAFER '
            '--config GDAL_CACHEMAX {max_memory}% '
            '-outsize 10% 0 '
            '"{geotiff_tmp}" "{geotiff_small}"'.format(**kwargs))

        # Fill scaled
        gdal_fillnodata(['.', 
                        '-co', 'NUM_THREADS=%s' % kwargs['threads'], 
                        '-co', 'BIGTIFF=IF_SAFER',
                        '--config', 'GDAL_CACHE_MAX', str(kwargs['max_memory']) + '%',
                        '-b', '1',
                        '-of', 'GTiff',
                        kwargs['geotiff_small'], kwargs['geotiff_small_filled']])
        
        # Merge filled scaled DEM with unfilled DEM using bilinear interpolation
        run('gdalbuildvrt -resolution highest -r bilinear "%s" "%s" "%s"' % (merged_vrt_path, geotiff_small_filled_path, geotiff_tmp_path))
        run('gdal_translate '
            '-co NUM_THREADS={threads} '
            '-co TILED=YES '
            '-co BIGTIFF=IF_SAFER '
            '-co COMPRESS=DEFLATE '
            '--config GDAL_CACHEMAX {max_memory}% '
            '"{merged_vrt}" "{geotiff}"'.format(**kwargs))
    else:
        run('gdal_translate '
                '-co NUM_THREADS={threads} '
                '-co TILED=YES '
                '-co BIGTIFF=IF_SAFER '
                '-co COMPRESS=DEFLATE '
                '--config GDAL_CACHEMAX {max_memory}% '
                '"{tiles_vrt}" "{geotiff}"'.format(**kwargs))

    if apply_smoothing:
        median_smoothing(geotiff_path, output_path, num_workers=max_workers)
        os.remove(geotiff_path)
    else:
        os.replace(geotiff_path, output_path)

    if os.path.exists(geotiff_tmp_path):
        if not keep_unfilled_copy: 
            os.remove(geotiff_tmp_path)
        else:
            os.replace(geotiff_tmp_path, io.related_file_path(output_path, postfix=".unfilled"))
    
    for cleanup_file in [tiles_vrt_path, tiles_file_list, merged_vrt_path, geotiff_small_path, geotiff_small_filled_path]:
        if os.path.exists(cleanup_file): os.remove(cleanup_file)
    for t in tiles:
        if os.path.exists(t['filename']): os.remove(t['filename'])

    log.ODM_INFO('Completed %s in %s' % (output_file, datetime.now() - start))


def compute_euclidean_map(geotiff_path, output_path, overwrite=False):
    if not os.path.exists(geotiff_path):
        log.ODM_WARNING("Cannot compute euclidean map (file does not exist: %s)" % geotiff_path)
        return

    nodata = -9999
    with rasterio.open(geotiff_path) as f:
        nodata = f.nodatavals[0]

    if not os.path.exists(output_path) or overwrite:
        log.ODM_INFO("Computing euclidean distance: %s" % output_path)

        if gdal_proximity is not None:
            try:
                gdal_proximity(['gdal_proximity.py', geotiff_path, output_path, '-values', str(nodata)])
            except Exception as e:
                log.ODM_WARNING("Cannot compute euclidean distance: %s" % str(e))

            if os.path.exists(output_path):
                return output_path
            else:
                log.ODM_WARNING("Cannot compute euclidean distance file: %s" % output_path)
        else:
            log.ODM_WARNING("Cannot compute euclidean map, gdal_proximity is missing")
            
    else:
        log.ODM_INFO("Found a euclidean distance map: %s" % output_path)
        return output_path


def median_smoothing(geotiff_path, output_path, smoothing_iterations=1, window_size=512, num_workers=1):
    """ Apply median smoothing """
    start = datetime.now()

    if not os.path.exists(geotiff_path):
        raise Exception('File %s does not exist!' % geotiff_path)

    # Prepare temporary files
    folder_path, output_filename = os.path.split(output_path)
    basename, ext = os.path.splitext(output_filename)

    output_dirty_in = os.path.join(folder_path, "{}.dirty_1{}".format(basename, ext))
    output_dirty_out = os.path.join(folder_path, "{}.dirty_2{}".format(basename, ext))

    log.ODM_INFO('Starting smoothing...')

    with rasterio.open(geotiff_path, num_threads=num_workers) as img, rasterio.open(output_dirty_in, "w+", BIGTIFF="IF_SAFER", num_threads=num_workers, **img.profile) as imgout, rasterio.open(output_dirty_out, "w+", BIGTIFF="IF_SAFER", num_threads=num_workers, **img.profile) as imgout2:
        nodata = img.nodatavals[0]
        dtype = img.dtypes[0]
        shape = img.shape
        for i in range(smoothing_iterations):
            log.ODM_INFO("Smoothing iteration %s" % str(i + 1))
            rows, cols = numpy.meshgrid(numpy.arange(0, shape[0], window_size), numpy.arange(0, shape[1], window_size))
            rows = rows.flatten()
            cols = cols.flatten()
            rows_end = numpy.minimum(rows + window_size, shape[0])
            cols_end= numpy.minimum(cols + window_size, shape[1])
            windows = numpy.dstack((rows, cols, rows_end, cols_end)).reshape(-1, 4)

            filt = functools.partial(ndimage.median_filter, size=9, output=dtype, mode='nearest')

            # We cannot read/write to the same file from multiple threads without causing race conditions. 
            # To safely read/write from multiple threads, we use a lock to protect the DatasetReader/Writer.
            read_lock = threading.Lock()
            write_lock = threading.Lock()

            # threading backend and GIL released filter are important for memory efficiency and multi-core performance
            Parallel(n_jobs=num_workers, backend='threading')(delayed(window_filter_2d)(img, imgout, nodata , window, 9, filt, read_lock, write_lock) for window in windows)

            # Between each iteration we swap the input and output temporary files
            #img_in, img_out = img_out, img_in
            if (i == 0):
                img = imgout
                imgout = imgout2
            else:
                img, imgout = imgout, img
    
    # If the number of iterations was even, we need to swap temporary files
    if (smoothing_iterations % 2 != 0):
        output_dirty_in, output_dirty_out = output_dirty_out, output_dirty_in

    # Cleaning temporary files
    if os.path.exists(output_dirty_out):
        os.replace(output_dirty_out, output_path)
    if os.path.exists(output_dirty_in):
        os.remove(output_dirty_in)

    log.ODM_INFO('Completed smoothing to create %s in %s' % (output_path, datetime.now() - start))
    return output_path


def window_filter_2d(img, imgout, nodata, window, kernel_size, filter, read_lock, write_lock):
    """
    Apply a filter to dem within a window, expects to work with kernal based filters

    :param img: path to the geotiff to filter
    :param imgout: path to write the giltered geotiff to. It can be the same as img to do the modification in place.
    :param window: the window to apply the filter, should be a list contains row start, col_start, row_end, col_end
    :param kernel_size: the size of the kernel for the filter, works with odd numbers, need to test if it works with even numbers
    :param filter: the filter function which takes a 2d array as input and filter results as output.
    :param read_lock: threading lock for the read operation
    :param write_lock: threading lock for the write operation
    """
    shape = img.shape[:2]
    if window[0] < 0 or window[1] < 0 or window[2] > shape[0] or window[3] > shape[1]:
        raise Exception('Window is out of bounds')
    expanded_window = [ max(0, window[0] - kernel_size // 2), max(0, window[1] - kernel_size // 2), min(shape[0], window[2] + kernel_size // 2), min(shape[1], window[3] + kernel_size // 2) ]

    # Read input window
    width = expanded_window[3] - expanded_window[1]
    height = expanded_window[2] - expanded_window[0]
    rasterio_window = rasterio.windows.Window(col_off=expanded_window[1], row_off=expanded_window[0], width=width, height=height)
    with read_lock:
        win_arr = img.read(indexes=1, window=rasterio_window)

    # Should have a better way to handle nodata, similar to the way the filter algorithms handle the border (reflection, nearest, interpolation, etc).
    # For now will follow the old approach to guarantee identical outputs
    nodata_locs = win_arr == nodata
    win_arr = filter(win_arr)
    win_arr[nodata_locs] = nodata
    win_arr = win_arr[window[0] - expanded_window[0] : window[2] - expanded_window[0], window[1] - expanded_window[1] : window[3] - expanded_window[1]]

    # Write output window
    width = window[3] - window[1]
    height = window[2] - window[0]
    rasterio_window = rasterio.windows.Window(col_off=window[1], row_off=window[0], width=width, height=height)
    with write_lock:
        imgout.write(win_arr, indexes=1, window=rasterio_window)


def get_dem_radius_steps(stats_file, steps, resolution, multiplier = 1.0):
    radius_steps = [point_cloud.get_spacing(stats_file, resolution) * multiplier]
    for _ in range(steps - 1):
        radius_steps.append(radius_steps[-1] * math.sqrt(2))
    
    return radius_steps