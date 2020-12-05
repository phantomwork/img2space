import os

from opendm import io
from opendm import log
from opendm import system
from opendm import context
from opendm import types
from opendm import gsd
from opendm import orthophoto
from opendm.concurrency import get_max_memory
from opendm.cutline import compute_cutline
from pipes import quote
from opendm import pseudogeo
from opendm.multispectral import get_primary_band_name

class ODMOrthoPhotoStage(types.ODM_Stage):
    def process(self, args, outputs):
        tree = outputs['tree']
        reconstruction = outputs['reconstruction']
        verbose = '-verbose' if args.verbose else ''

        # define paths and create working directories
        system.mkdir_p(tree.odm_orthophoto)

        if not io.file_exists(tree.odm_orthophoto_tif) or self.rerun():
            gsd_error_estimate = 0.1
            ignore_resolution = False
            if not reconstruction.is_georeferenced():
                # Match DEMs
                gsd_error_estimate = -3
                ignore_resolution = True

            resolution = 1.0 / (gsd.cap_resolution(args.orthophoto_resolution, tree.opensfm_reconstruction,
                                                    gsd_error_estimate=gsd_error_estimate, 
                                                    ignore_gsd=args.ignore_gsd,
                                                    ignore_resolution=ignore_resolution,
                                                    has_gcp=reconstruction.has_gcp()) / 100.0)

            # odm_orthophoto definitions
            kwargs = {
                'bin': context.odm_modules_path,
                'log': tree.odm_orthophoto_log,
                'ortho': tree.odm_orthophoto_render,
                'corners': tree.odm_orthophoto_corners,
                'res': resolution,
                'bands': '',
                'verbose': verbose
            }

            # Check if the georef object is initialized
            # (during a --rerun this might not be)
            # TODO: this should be moved to a more central location?
            if reconstruction.is_georeferenced() and not reconstruction.georef.valid_utm_offsets():
                georeferencing_dir = tree.odm_georeferencing if args.use_3dmesh and not args.skip_3dmodel else tree.odm_25dgeoreferencing
                odm_georeferencing_model_txt_geo_file = os.path.join(georeferencing_dir, tree.odm_georeferencing_model_txt_geo)

                if io.file_exists(odm_georeferencing_model_txt_geo_file):
                    reconstruction.georef.extract_offsets(odm_georeferencing_model_txt_geo_file)
                else:
                    log.ODM_WARNING('Cannot read UTM offset from {}.'.format(odm_georeferencing_model_txt_geo_file))

            models = []

            if args.use_3dmesh:
                base_dir = tree.odm_texturing
            else:
                base_dir = tree.odm_25dtexturing
                
            if reconstruction.is_georeferenced():
                model_file = tree.odm_georeferencing_model_obj_geo
            else:
                model_file = tree.odm_textured_model_obj

            if reconstruction.multi_camera:
                for band in reconstruction.multi_camera:
                    primary = band['name'] == get_primary_band_name(reconstruction.multi_camera, args.primary_band)
                    subdir = ""
                    if not primary:
                        subdir = band['name'].lower()
                    models.append(os.path.join(base_dir, subdir, model_file))
                kwargs['bands'] = '-bands %s' % (','.join([quote(b['name'].lower()) for b in reconstruction.multi_camera]))
            else:
                models.append(os.path.join(base_dir, model_file))

            kwargs['models'] = ','.join(map(quote, models))

            # run odm_orthophoto
            system.run('{bin}/odm_orthophoto -inputFiles {models} '
                       '-logFile {log} -outputFile {ortho} -resolution {res} {verbose} '
                       '-outputCornerFile {corners} {bands}'.format(**kwargs))

            # Create georeferenced GeoTiff
            geotiffcreated = False

            if reconstruction.is_georeferenced() and reconstruction.georef.valid_utm_offsets():
                ulx = uly = lrx = lry = 0.0
                with open(tree.odm_orthophoto_corners) as f:
                    for lineNumber, line in enumerate(f):
                        if lineNumber == 0:
                            tokens = line.split(' ')
                            if len(tokens) == 4:
                                ulx = float(tokens[0]) + \
                                    float(reconstruction.georef.utm_east_offset)
                                lry = float(tokens[1]) + \
                                    float(reconstruction.georef.utm_north_offset)
                                lrx = float(tokens[2]) + \
                                    float(reconstruction.georef.utm_east_offset)
                                uly = float(tokens[3]) + \
                                    float(reconstruction.georef.utm_north_offset)
                log.ODM_INFO('Creating GeoTIFF')

                orthophoto_vars = orthophoto.get_orthophoto_vars(args)

                kwargs = {
                    'ulx': ulx,
                    'uly': uly,
                    'lrx': lrx,
                    'lry': lry,
                    'vars': ' '.join(['-co %s=%s' % (k, orthophoto_vars[k]) for k in orthophoto_vars]),
                    'proj': reconstruction.georef.proj4(),
                    'input': tree.odm_orthophoto_render,
                    'output': tree.odm_orthophoto_tif,
                    'log': tree.odm_orthophoto_tif_log,
                    'max_memory': get_max_memory(),
                }

                system.run('gdal_translate -a_ullr {ulx} {uly} {lrx} {lry} '
                           '{vars} '
                           '-a_srs \"{proj}\" '
                           '--config GDAL_CACHEMAX {max_memory}% '
                           '--config GDAL_TIFF_INTERNAL_MASK YES '
                           '{input} {output} > {log}'.format(**kwargs))

                bounds_file_path = os.path.join(tree.odm_georeferencing, 'odm_georeferenced_model.bounds.gpkg')
                    
                # Cutline computation, before cropping
                # We want to use the full orthophoto, not the cropped one.
                if args.orthophoto_cutline:
                    cutline_file = os.path.join(tree.odm_orthophoto, "cutline.gpkg")

                    compute_cutline(tree.odm_orthophoto_tif, 
                                    bounds_file_path,
                                    cutline_file,
                                    args.max_concurrency,
                                    tmpdir=os.path.join(tree.odm_orthophoto, "grass_cutline_tmpdir"),
                                    scale=0.25)

                    orthophoto.compute_mask_raster(tree.odm_orthophoto_tif, cutline_file, 
                                           os.path.join(tree.odm_orthophoto, "odm_orthophoto_cut.tif"),
                                           blend_distance=20, only_max_coords_feature=True)

                orthophoto.post_orthophoto_steps(args, bounds_file_path, tree.odm_orthophoto_tif, tree.orthophoto_tiles)

                # Generate feathered orthophoto also
                if args.orthophoto_cutline:
                    orthophoto.feather_raster(tree.odm_orthophoto_tif, 
                            os.path.join(tree.odm_orthophoto, "odm_orthophoto_feathered.tif"),
                            blend_distance=20
                        )

                geotiffcreated = True
            if not geotiffcreated:
                if io.file_exists(tree.odm_orthophoto_render):
                    pseudogeo.add_pseudo_georeferencing(tree.odm_orthophoto_render)
                    log.ODM_INFO("Renaming %s --> %s" % (tree.odm_orthophoto_render, tree.odm_orthophoto_tif))
                    os.rename(tree.odm_orthophoto_render, tree.odm_orthophoto_tif)
                else:
                    log.ODM_WARNING("Could not generate an orthophoto (it did not render)")
        else:
            log.ODM_WARNING('Found a valid orthophoto in: %s' % tree.odm_orthophoto_tif)

        if args.optimize_disk_space and io.file_exists(tree.odm_orthophoto_render):
            os.remove(tree.odm_orthophoto_render)
