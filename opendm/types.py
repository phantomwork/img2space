import cv2
import exifread
import re
import os
from fractions import Fraction
from opensfm.exif import sensor_string
from opendm import get_image_size
from opendm import location
from opendm.gcp import GCPFile
from pyproj import CRS

import log
import io
import system
import context
import logging
from opendm.progress import progressbc

class ODM_Photo:
    """   ODMPhoto - a class for ODMPhotos
    """

    def __init__(self, path_file):
        #  general purpose
        self.filename = io.extract_file_from_path_file(path_file)
        self.width = None
        self.height = None
        # other attributes
        self.camera_make = ''
        self.camera_model = ''
        self.make_model = ''
        self.latitude = None
        self.longitude = None
        self.altitude = None
        # parse values from metadata
        self.parse_exif_values(path_file)

        # print log message
        log.ODM_DEBUG('Loaded {}'.format(self))


    def __str__(self):
        return '{} | camera: {} | dimensions: {} x {} | lat: {} | lon: {} | alt: {}'.format(
                            self.filename, self.make_model, self.width, self.height, self.latitude, self.longitude, self.altitude)

    def parse_exif_values(self, _path_file):
        # Disable exifread log
        logging.getLogger('exifread').setLevel(logging.CRITICAL)

        with open(_path_file, 'rb') as f:
            tags = exifread.process_file(f, details=False)

            try:
                if 'Image Make' in tags:
                    self.camera_make = tags['Image Make'].values.encode('utf8')
                if 'Image Model' in tags:
                    self.camera_model = tags['Image Model'].values.encode('utf8')
                if 'GPS GPSAltitude' in tags:
                    self.altitude = self.float_values(tags['GPS GPSAltitude'])[0]
                    if 'GPS GPSAltitudeRef' in tags and self.int_values(tags['GPS GPSAltitudeRef'])[0] > 0:
                        self.altitude *= -1
                if 'GPS GPSLatitude' in tags and 'GPS GPSLatitudeRef' in tags:
                    self.latitude = self.dms_to_decimal(tags['GPS GPSLatitude'], tags['GPS GPSLatitudeRef'])
                if 'GPS GPSLongitude' in tags and 'GPS GPSLongitudeRef' in tags:
                    self.longitude = self.dms_to_decimal(tags['GPS GPSLongitude'], tags['GPS GPSLongitudeRef'])
            except IndexError as e:
                log.ODM_WARNING("Cannot read EXIF tags for %s: %s" % (_path_file, e.message))

        if self.camera_make and self.camera_model:
            self.make_model = sensor_string(self.camera_make, self.camera_model)

        self.width, self.height = get_image_size.get_image_size(_path_file)

    def dms_to_decimal(self, dms, sign):
        """Converts dms coords to decimal degrees"""
        degrees, minutes, seconds = self.float_values(dms)

        return (-1 if sign.values[0] in 'SWsw' else 1) * (
            degrees +
            minutes / 60 +
            seconds / 3600
        )

    def float_values(self, tag):
        return map(lambda v: float(v.num) / float(v.den), tag.values) 

    def int_values(self, tag):
        return map(int, tag.values)


class ODM_Reconstruction(object):
    def __init__(self, photos):
        self.photos = photos
        self.georef = None
        self.gcp = None

    def is_georeferenced(self):
        return self.georef is not None

    def georeference_with_gcp(self, gcp_file, output_coords_file, output_gcp_file, rerun=False):
        if not io.file_exists(output_coords_file) or not io.file_exists(output_gcp_file) or rerun:
            gcp = GCPFile(gcp_file)
            if gcp.exists():
                # Create coords file, we'll be using this later
                # during georeferencing
                with open(output_coords_file, 'w') as f:
                    coords_header = gcp.wgs84_utm_zone()
                    f.write(coords_header + "\n")
                    log.ODM_INFO("Generated coords file from GCP: %s" % coords_header)

                # Convert GCP file to a UTM projection since the rest of the pipeline
                # does not handle other SRS well.
                rejected_entries = []
                utm_gcp = GCPFile(gcp.create_utm_copy(output_gcp_file, filenames=[p.filename for p in self.photos], rejected_entries=rejected_entries, include_extras=False))
                
                if not utm_gcp.exists():
                    raise RuntimeError("Could not project GCP file to UTM. Please double check your GCP file for mistakes.")
                
                for re in rejected_entries:
                    log.ODM_WARNING("GCP line ignored (image not found): %s" % str(re))
                
                if utm_gcp.entries_count() > 0:
                    log.ODM_INFO("%s GCP points will be used for georeferencing" % utm_gcp.entries_count())
                else:
                    raise RuntimeError("A GCP file was provided, but no valid GCP entries could be used. Note that the GCP file is case sensitive (\".JPG\" is not the same as \".jpg\").")

                self.gcp = utm_gcp
            else:
                log.ODM_WARNING("GCP file does not exist: %s" % gcp_file)
                return
        else:
            log.ODM_INFO("Coordinates file already exist: %s" % output_coords_file)
            log.ODM_INFO("GCP file already exist: %s" % output_gcp_file)
            self.gcp = GCPFile(output_gcp_file)
        
        self.georef = ODM_GeoRef.FromCoordsFile(output_coords_file)
        return self.georef

    def georeference_with_gps(self, images_path, output_coords_file, rerun=False):
        try:
            if not io.file_exists(output_coords_file) or rerun:
                location.extract_utm_coords(self.photos, images_path, output_coords_file)
            else:
                log.ODM_INFO("Coordinates file already exist: %s" % output_coords_file)
            
            self.georef = ODM_GeoRef.FromCoordsFile(output_coords_file)
        except:
            log.ODM_WARNING('Could not generate coordinates file. An orthophoto will not be generated.')

        self.gcp = GCPFile(None)
        return self.georef

    def save_proj_srs(self, file):
        # Save proj to file for future use (unless this 
        # dataset is not georeferenced)
        if self.is_georeferenced():
            with open(file, 'w') as f:
                f.write(self.georef.proj4())

class ODM_GeoRef(object):
    @staticmethod
    def FromProj(projstring):
        return ODM_GeoRef(CRS.from_proj4(projstring))

    @staticmethod
    def FromCoordsFile(coords_file):
        # check for coordinate file existence
        if not io.file_exists(coords_file):
            log.ODM_WARNING('Could not find file %s' % coords_file)
            return

        srs = None

        with open(coords_file) as f:
            # extract reference system and utm zone from first line.
            # We will assume the following format:
            # 'WGS84 UTM 17N' or 'WGS84 UTM 17N \n'
            line = f.readline().rstrip()
            srs = location.parse_srs_header(line)

        return ODM_GeoRef(srs)

    def __init__(self, srs):
        self.srs = srs
        self.utm_east_offset = 0
        self.utm_north_offset = 0
        self.transform = []

    def proj4(self):
        return self.srs.to_proj4()
    
    def valid_utm_offsets(self):
        return self.utm_east_offset and self.utm_north_offset

    def extract_offsets(self, geo_sys_file):
        if not io.file_exists(geo_sys_file):
            log.ODM_ERROR('Could not find file %s' % geo_sys_file)
            return

        with open(geo_sys_file) as f:
            offsets = f.readlines()[1].split(' ')
            self.utm_east_offset = float(offsets[0])
            self.utm_north_offset = float(offsets[1])

    def parse_transformation_matrix(self, matrix_file):
        if not io.file_exists(matrix_file):
            log.ODM_ERROR('Could not find file %s' % matrix_file)
            return

        # Create a nested list for the transformation matrix
        with open(matrix_file) as f:
            for line in f:
                # Handle matrix formats that either
                # have leading or trailing brakets or just plain numbers.
                line = re.sub(r"[\[\],]", "", line).strip()
                self.transform += [[float(i) for i in line.split()]]

        self.utm_east_offset = self.transform[0][3]
        self.utm_north_offset = self.transform[1][3]


class ODM_Tree(object):
    def __init__(self, root_path, gcp_file = None):
        # root path to the project
        self.root_path = io.absolute_path_file(root_path)
        self.input_images = io.join_paths(self.root_path, 'images')

        # modules paths

        # here are defined where all modules should be located in
        # order to keep track all files al directories during the
        # whole reconstruction process.
        self.dataset_raw = io.join_paths(self.root_path, 'images')
        self.opensfm = io.join_paths(self.root_path, 'opensfm')
        self.mve = io.join_paths(self.root_path, 'mve')
        self.odm_meshing = io.join_paths(self.root_path, 'odm_meshing')
        self.odm_texturing = io.join_paths(self.root_path, 'odm_texturing')
        self.odm_25dtexturing = io.join_paths(self.root_path, 'odm_texturing_25d')
        self.odm_georeferencing = io.join_paths(self.root_path, 'odm_georeferencing')
        self.odm_25dgeoreferencing = io.join_paths(self.root_path, 'odm_25dgeoreferencing')
        self.odm_filterpoints = io.join_paths(self.root_path, 'odm_filterpoints')
        self.odm_orthophoto = io.join_paths(self.root_path, 'odm_orthophoto')

        # important files paths

        # benchmarking
        self.benchmarking = io.join_paths(self.root_path, 'benchmark.txt')
        self.dataset_list = io.join_paths(self.root_path, 'img_list.txt')

        # opensfm
        self.opensfm_tracks = io.join_paths(self.opensfm, 'tracks.csv')
        self.opensfm_bundle = io.join_paths(self.opensfm, 'bundle_r000.out')
        self.opensfm_bundle_list = io.join_paths(self.opensfm, 'list_r000.out')
        self.opensfm_image_list = io.join_paths(self.opensfm, 'image_list.txt')
        self.opensfm_reconstruction = io.join_paths(self.opensfm, 'reconstruction.json')
        self.opensfm_reconstruction_nvm = io.join_paths(self.opensfm, 'reconstruction.nvm')
        self.opensfm_model = io.join_paths(self.opensfm, 'depthmaps/merged.ply')
        self.opensfm_transformation = io.join_paths(self.opensfm, 'geocoords_transformation.txt')

        # mve
        self.mve_model = io.join_paths(self.mve, 'mve_dense_point_cloud.ply')
        self.mve_path = io.join_paths(self.opensfm, 'mve')
        self.mve_image_list = io.join_paths(self.mve_path, 'list.txt')
        self.mve_bundle = io.join_paths(self.mve_path, 'bundle/bundle.out')
        self.mve_views = io.join_paths(self.mve, 'views')

        # filter points
        self.filtered_point_cloud = io.join_paths(self.odm_filterpoints, "point_cloud.ply")

        # odm_meshing
        self.odm_mesh = io.join_paths(self.odm_meshing, 'odm_mesh.ply')
        self.odm_meshing_log = io.join_paths(self.odm_meshing, 'odm_meshing_log.txt')
        self.odm_25dmesh = io.join_paths(self.odm_meshing, 'odm_25dmesh.ply')
        self.odm_25dmeshing_log = io.join_paths(self.odm_meshing, 'odm_25dmeshing_log.txt')

        # texturing
        self.odm_texturing_undistorted_image_path = io.join_paths(
            self.odm_texturing, 'undistorted')
        self.odm_textured_model_obj = 'odm_textured_model.obj'
        self.odm_textured_model_mtl = 'odm_textured_model.mtl'
        # Log is only used by old odm_texturing
        self.odm_texuring_log = 'odm_texturing_log.txt'

        # odm_georeferencing
        self.odm_georeferencing_latlon = io.join_paths(
            self.odm_georeferencing, 'latlon.txt')
        self.odm_georeferencing_coords = io.join_paths(
            self.odm_georeferencing, 'coords.txt')
        self.odm_georeferencing_gcp = gcp_file or io.find('gcp_list.txt', self.root_path)
        self.odm_georeferencing_gcp_utm = io.join_paths(self.odm_georeferencing, 'gcp_list_utm.txt')
        self.odm_georeferencing_utm_log = io.join_paths(
            self.odm_georeferencing, 'odm_georeferencing_utm_log.txt')
        self.odm_georeferencing_log = 'odm_georeferencing_log.txt'
        self.odm_georeferencing_transform_file = 'odm_georeferencing_transform.txt'
        self.odm_georeferencing_proj = 'proj.txt'
        self.odm_georeferencing_model_txt_geo = 'odm_georeferencing_model_geo.txt'
        self.odm_georeferencing_model_obj_geo = 'odm_textured_model_geo.obj'
        self.odm_georeferencing_xyz_file = io.join_paths(
            self.odm_georeferencing, 'odm_georeferenced_model.csv')
        self.odm_georeferencing_las_json = io.join_paths(
            self.odm_georeferencing, 'las.json')
        self.odm_georeferencing_model_laz = io.join_paths(
            self.odm_georeferencing, 'odm_georeferenced_model.laz')
        self.odm_georeferencing_model_las = io.join_paths(
            self.odm_georeferencing, 'odm_georeferenced_model.las')
        self.odm_georeferencing_dem = io.join_paths(
            self.odm_georeferencing, 'odm_georeferencing_model_dem.tif')

        # odm_orthophoto
        self.odm_orthophoto_file = io.join_paths(self.odm_orthophoto, 'odm_orthophoto.png')
        self.odm_orthophoto_tif = io.join_paths(self.odm_orthophoto, 'odm_orthophoto.tif')
        self.odm_orthophoto_corners = io.join_paths(self.odm_orthophoto, 'odm_orthophoto_corners.txt')
        self.odm_orthophoto_log = io.join_paths(self.odm_orthophoto, 'odm_orthophoto_log.txt')
        self.odm_orthophoto_tif_log = io.join_paths(self.odm_orthophoto, 'gdal_translate_log.txt')

        # Split-merge 
        self.submodels_path = io.join_paths(self.root_path, 'submodels')

        # Tiles
        self.entwine_pointcloud = self.path("entwine_pointcloud")

    def path(self, *args):
        return os.path.join(self.root_path, *args)


class ODM_Stage:
    def __init__(self, name, args, progress=0.0, **params):
        self.name = name
        self.args = args
        self.progress = progress
        self.params = params
        if self.params is None:
            self.params = {}
        self.next_stage = None
        self.prev_stage = None

    def connect(self, stage):
        self.next_stage = stage
        stage.prev_stage = self
        return stage

    def rerun(self):
        """
        Does this stage need to be rerun?
        """
        return (self.args.rerun is not None and self.args.rerun == self.name) or \
                     (self.args.rerun_all) or \
                     (self.args.rerun_from is not None and self.name in self.args.rerun_from)
    
    def run(self, outputs = {}):
        start_time = system.now_raw()
        log.ODM_INFO('Running %s stage' % self.name)

        self.process(self.args, outputs)

        # The tree variable should always be populated at this point
        if outputs.get('tree') is None:
            raise Exception("Assert violation: tree variable is missing from outputs dictionary.")

        if self.args.time:
            system.benchmark(start_time, outputs['tree'].benchmarking, self.name)

        log.ODM_INFO('Finished %s stage' % self.name)
        self.update_progress_end()

        # Last stage?
        if self.args.end_with == self.name or self.args.rerun == self.name:
            log.ODM_INFO("No more stages to run")
            return

        # Run next stage?
        elif self.next_stage is not None:
            self.next_stage.run(outputs)

    def delta_progress(self):
        if self.prev_stage:
            return max(0.0, self.progress - self.prev_stage.progress)
        else:
            return max(0.0, self.progress)
    
    def previous_stages_progress(self):
        if self.prev_stage:
            return max(0.0, self.prev_stage.progress)
        else:
            return 0.0

    def update_progress_end(self):
        self.update_progress(100.0)

    def update_progress(self, progress):
        progress = max(0.0, min(100.0, progress))
        progressbc.send_update(self.previous_stages_progress() + 
                              (self.delta_progress() / 100.0) * float(progress))

    def process(self, args, outputs):
        raise NotImplementedError

