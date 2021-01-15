import sys
import os
import shutil
import glob

from opendm import log
from opendm import io
from opendm import system
from opendm import context
from opendm import gsd
from opendm import point_cloud
from opendm import types
from opendm.utils import get_depthmap_resolution
from opendm.osfm import OSFMContext
from opendm import multispectral
from opendm import nvm

class ODMOpenSfMStage(types.ODM_Stage):
    def process(self, args, outputs):
        tree = outputs['tree']
        reconstruction = outputs['reconstruction']
        photos = reconstruction.photos

        if not photos:
            log.ODM_ERROR('Not enough photos in photos array to start OpenSfM')
            exit(1)

        octx = OSFMContext(tree.opensfm)
        octx.setup(args, tree.dataset_raw, reconstruction=reconstruction, rerun=self.rerun())
        octx.extract_metadata(self.rerun())
        self.update_progress(20)
        octx.feature_matching(self.rerun())
        self.update_progress(30)
        octx.reconstruct(self.rerun())
        octx.extract_cameras(tree.path("cameras.json"), self.rerun())
        self.update_progress(70)

        def cleanup_disk_space():
            if args.optimize_disk_space:
                for folder in ["features", "matches", "exif", "reports"]:
                    folder_path = octx.path(folder)
                    if os.path.islink(folder_path):
                        os.unlink(folder_path)
                    else:
                        shutil.rmtree(folder_path)

        # If we find a special flag file for split/merge we stop right here
        if os.path.exists(octx.path("split_merge_stop_at_reconstruction.txt")):
            log.ODM_INFO("Stopping OpenSfM early because we found: %s" % octx.path("split_merge_stop_at_reconstruction.txt"))
            self.next_stage = None
            cleanup_disk_space()
            return

        updated_config_flag_file = octx.path('updated_config.txt')

        # Make sure it's capped by the depthmap-resolution arg,
        # since the undistorted images are used for MVS
        outputs['undist_image_max_size'] = max(
            gsd.image_max_size(photos, args.orthophoto_resolution, tree.opensfm_reconstruction, ignore_gsd=args.ignore_gsd, has_gcp=reconstruction.has_gcp()),
            get_depthmap_resolution(args, photos)
        )

        if not io.file_exists(updated_config_flag_file) or self.rerun():
            octx.update_config({'undistorted_image_max_size': outputs['undist_image_max_size']})
            octx.touch(updated_config_flag_file)

        # Undistorted images will be used for texturing / MVS

        alignment_info = None
        primary_band_name = None
        undistort_pipeline = []

        def undistort_callback(shot_id, image):
            for func in undistort_pipeline:
                image = func(shot_id, image)
            return image

        def radiometric_calibrate(shot_id, image):
            photo = reconstruction.get_photo(shot_id)
            return multispectral.dn_to_reflectance(photo, image, use_sun_sensor=args.radiometric_calibration=="camera+sun")


        def align_to_primary_band(shot_id, image):
            photo = reconstruction.get_photo(shot_id)

            # No need to align if requested by user
            if args.skip_band_alignment:
                return image

            # No need to align primary
            if photo.band_name == primary_band_name:
                return image

            ainfo = alignment_info.get(photo.band_name)
            if ainfo is not None:
                return multispectral.align_image(image, ainfo['warp_matrix'], ainfo['dimension'])
            else:
                log.ODM_WARNING("Cannot align %s, no alignment matrix could be computed. Band alignment quality might be affected." % (shot_id))
                return image

        if args.radiometric_calibration != "none":
            undistort_pipeline.append(radiometric_calibrate)
        
        image_list_override = None

        if reconstruction.multi_camera:
            
            # Undistort only secondary bands
            image_list_override = [os.path.join(tree.dataset_raw, p.filename) for p in photos] # if p.band_name.lower() != primary_band_name.lower()

            # We backup the original reconstruction.json, tracks.csv
            # then we augment them by duplicating the primary band
            # camera shots with each band, so that exports, undistortion,
            # etc. include all bands
            # We finally restore the original files later

            added_shots_file = octx.path('added_shots_done.txt')

            if not io.file_exists(added_shots_file) or self.rerun():
                primary_band_name = multispectral.get_primary_band_name(reconstruction.multi_camera, args.primary_band)
                s2p, p2s = multispectral.compute_band_maps(reconstruction.multi_camera, primary_band_name)
                
                if not args.skip_band_alignment:
                    alignment_info = multispectral.compute_alignment_matrices(reconstruction.multi_camera, primary_band_name, tree.dataset_raw, s2p, p2s, max_concurrency=args.max_concurrency)
                else:
                    log.ODM_WARNING("Skipping band alignment")
                    alignment_info = {}
                    
                log.ODM_INFO("Adding shots to reconstruction")
                
                octx.backup_reconstruction()
                octx.add_shots_to_reconstruction(p2s)
                octx.touch(added_shots_file)

            undistort_pipeline.append(align_to_primary_band)

        octx.convert_and_undistort(self.rerun(), undistort_callback, image_list_override)

        self.update_progress(80)

        if reconstruction.multi_camera:
            octx.restore_reconstruction_backup()

            # Undistort primary band and write undistorted 
            # reconstruction.json, tracks.csv
            octx.convert_and_undistort(self.rerun(), undistort_callback, runId='primary')

        if not io.file_exists(tree.opensfm_reconstruction_nvm) or self.rerun():
            octx.run('export_visualsfm --points')
        else:
            log.ODM_WARNING('Found a valid OpenSfM NVM reconstruction file in: %s' %
                            tree.opensfm_reconstruction_nvm)
        
        if reconstruction.multi_camera:
            log.ODM_INFO("Multiple bands found")

            # Write NVM files for the various bands
            for band in reconstruction.multi_camera:
                nvm_file = octx.path("undistorted", "reconstruction_%s.nvm" % band['name'].lower())

                if not io.file_exists(nvm_file) or self.rerun():
                    img_map = {}

                    if primary_band_name is None:
                        primary_band_name = multispectral.get_primary_band_name(reconstruction.multi_camera, args.primary_band)
                    if p2s is None:
                        s2p, p2s = multispectral.compute_band_maps(reconstruction.multi_camera, primary_band_name)
                    
                    for fname in p2s:
                        
                        # Primary band maps to itself
                        if band['name'] == primary_band_name:
                            img_map[fname + '.tif'] = fname + '.tif'
                        else:
                            band_filename = next((p.filename for p in p2s[fname] if p.band_name == band['name']), None)

                            if band_filename is not None:
                                img_map[fname + '.tif'] = band_filename + '.tif'
                            else:
                                log.ODM_WARNING("Cannot find %s band equivalent for %s" % (band, fname))

                    nvm.replace_nvm_images(tree.opensfm_reconstruction_nvm, img_map, nvm_file)
                else:
                    log.ODM_WARNING("Found existing NVM file %s" % nvm_file)
                    
        self.update_progress(85)

        # Skip dense reconstruction if necessary and export
        # sparse reconstruction instead
        if args.fast_orthophoto:
            output_file = octx.path('reconstruction.ply')

            if not io.file_exists(output_file) or self.rerun():
                octx.run('export_ply --no-cameras --point-num-views')
            else:
                log.ODM_WARNING("Found a valid PLY reconstruction in %s" % output_file)

        self.update_progress(90)

        if reconstruction.is_georeferenced() and (not io.file_exists(tree.opensfm_transformation) or self.rerun()):
            octx.run('export_geocoords --transformation --proj \'%s\'' % reconstruction.georef.proj4())
        else:
            log.ODM_WARNING("Will skip exporting %s" % tree.opensfm_transformation)
        
        self.update_progress(95)

        if not args.skip_report:
            octx.export_stats(self.rerun())

        cleanup_disk_space()

        if args.optimize_disk_space:
            os.remove(octx.path("tracks.csv"))
            if io.file_exists(octx.recon_backup_file()):
                os.remove(octx.recon_backup_file())

            if io.dir_exists(octx.path("undistorted", "depthmaps")):
                files = glob.glob(octx.path("undistorted", "depthmaps", "*.npz"))
                for f in files:
                    os.remove(f)

            # Keep these if using OpenMVS
            if args.fast_orthophoto:
                files = [octx.path("undistorted", "tracks.csv"),
                         octx.path("undistorted", "reconstruction.json")
                        ]
                for f in files:
                    if os.path.exists(f):
                        os.remove(f)