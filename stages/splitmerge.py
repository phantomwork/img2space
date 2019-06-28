import os
import shutil
from opendm import log
from opendm.osfm import OSFMContext, get_submodel_argv, get_submodel_paths, get_all_submodel_paths
from opendm import types
from opendm import io
from opendm import system
from opendm import orthophoto
from opendm.gcp import GCPFile
from opendm.dem import pdal, utils
from opendm.dem.merge import euclidean_merge_dems
from opensfm.large import metadataset
from opendm.cropper import Cropper
from opendm.concurrency import get_max_memory
from opendm.remote import LocalRemoteExecutor
from opendm import entwine
from pipes import quote

class ODMSplitStage(types.ODM_Stage):
    def process(self, args, outputs):
        tree = outputs['tree']
        reconstruction = outputs['reconstruction']
        photos = reconstruction.photos

        outputs['large'] = len(photos) > args.split

        if outputs['large']:
            # If we have a cluster address, we'll use a distributed workflow
            local_workflow = not bool(args.sm_cluster)

            octx = OSFMContext(tree.opensfm)
            split_done_file = octx.path("split_done.txt")

            if not io.file_exists(split_done_file) or self.rerun():
                orig_max_concurrency = args.max_concurrency
                if not local_workflow:
                    args.max_concurrency = max(1, args.max_concurrency - 1)
                    log.ODM_INFO("Setting max-concurrency to %s to better handle remote splits" % args.max_concurrency)

                log.ODM_INFO("Large dataset detected (%s photos) and split set at %s. Preparing split merge." % (len(photos), args.split))
                config = [
                    "submodels_relpath: ../submodels/opensfm",
                    "submodel_relpath_template: ../submodels/submodel_%04d/opensfm",
                    "submodel_images_relpath_template: ../submodels/submodel_%04d/images",
                    "submodel_size: %s" % args.split,
                    "submodel_overlap: %s" % args.split_overlap,
                ]

                octx.setup(args, tree.dataset_raw, photos, gcp_path=reconstruction.gcp.gcp_path, append_config=config, rerun=self.rerun())
                octx.extract_metadata(self.rerun())

                self.update_progress(5)

                if local_workflow:
                    octx.feature_matching(self.rerun())

                self.update_progress(20)

                # Create submodels
                if not io.dir_exists(tree.submodels_path) or self.rerun():
                    if io.dir_exists(tree.submodels_path):
                        log.ODM_WARNING("Removing existing submodels directory: %s" % tree.submodels_path)
                        shutil.rmtree(tree.submodels_path)

                    octx.run("create_submodels")
                else:
                    log.ODM_WARNING("Submodels directory already exist at: %s" % tree.submodels_path)

                # Find paths of all submodels
                mds = metadataset.MetaDataSet(tree.opensfm)
                submodel_paths = [os.path.abspath(p) for p in mds.get_submodel_paths()]

                for sp in submodel_paths:
                    sp_octx = OSFMContext(sp)

                    # Copy filtered GCP file if needed
                    # One in OpenSfM's directory, one in the submodel project directory
                    if reconstruction.gcp and reconstruction.gcp.exists():
                        submodel_gcp_file = os.path.abspath(sp_octx.path("..", "gcp_list.txt"))
                        submodel_images_dir = os.path.abspath(sp_octx.path("..", "images"))

                        if reconstruction.gcp.make_filtered_copy(submodel_gcp_file, submodel_images_dir):
                            log.ODM_INFO("Copied filtered GCP file to %s" % submodel_gcp_file)
                            io.copy(submodel_gcp_file, os.path.abspath(sp_octx.path("gcp_list.txt")))
                        else:
                            log.ODM_INFO("No GCP will be copied for %s, not enough images in the submodel are referenced by the GCP" % sp_octx.name())
                        
                # Reconstruct each submodel
                log.ODM_INFO("Dataset has been split into %s submodels. Reconstructing each submodel..." % len(submodel_paths))
                self.update_progress(25)

                if local_workflow:
                    for sp in submodel_paths:
                        log.ODM_INFO("Reconstructing %s" % sp)
                        OSFMContext(sp).reconstruct(self.rerun())
                else:
                    lre = LocalRemoteExecutor(args.sm_cluster, self.rerun())
                    lre.set_projects([os.path.abspath(os.path.join(p, "..")) for p in submodel_paths])
                    lre.run_reconstruction()

                self.update_progress(50)

                # Align
                octx.align_reconstructions(self.rerun())

                self.update_progress(55)

                # Aligned reconstruction is in reconstruction.aligned.json
                # We need to rename it to reconstruction.json
                remove_paths = []
                for sp in submodel_paths:
                    sp_octx = OSFMContext(sp)

                    aligned_recon = sp_octx.path('reconstruction.aligned.json')
                    unaligned_recon = sp_octx.path('reconstruction.unaligned.json')
                    main_recon = sp_octx.path('reconstruction.json')

                    if io.file_exists(main_recon) and io.file_exists(unaligned_recon) and not self.rerun():
                        log.ODM_INFO("Submodel %s has already been aligned." % sp_octx.name())
                        continue

                    if not io.file_exists(aligned_recon):
                        log.ODM_WARNING("Submodel %s does not have an aligned reconstruction (%s). "
                                        "This could mean that the submodel could not be reconstructed "
                                        " (are there enough features to reconstruct it?). Skipping." % (sp_octx.name(), aligned_recon))
                        remove_paths.append(sp)
                        continue

                    if io.file_exists(main_recon):
                        shutil.move(main_recon, unaligned_recon)

                    shutil.move(aligned_recon, main_recon)
                    log.ODM_INFO("%s is now %s" % (aligned_recon, main_recon))

                # Remove invalid submodels
                submodel_paths = [p for p in submodel_paths if not p in remove_paths]

                # Run ODM toolchain for each submodel
                if local_workflow:
                    for sp in submodel_paths:
                        sp_octx = OSFMContext(sp)

                        log.ODM_INFO("========================")
                        log.ODM_INFO("Processing %s" % sp_octx.name()) 
                        log.ODM_INFO("========================")

                        argv = get_submodel_argv(args.name, tree.submodels_path, sp_octx.name())

                        # Re-run the ODM toolchain on the submodel
                        system.run(" ".join(map(quote, argv)), env_vars=os.environ.copy())
                else:
                    lre.set_projects([os.path.abspath(os.path.join(p, "..")) for p in submodel_paths])
                    lre.run_toolchain()

                # Restore max_concurrency value
                args.max_concurrency = orig_max_concurrency

                octx.touch(split_done_file)
            else:
                log.ODM_WARNING('Found a split done file in: %s' % split_done_file)
        else:
            log.ODM_INFO("Normal dataset, will process all at once.")
            self.progress = 0.0


class ODMMergeStage(types.ODM_Stage):
    def process(self, args, outputs):
        tree = outputs['tree']
        reconstruction = outputs['reconstruction']

        if outputs['large']:
            if not os.path.exists(tree.submodels_path):
                log.ODM_ERROR("We reached the merge stage, but %s folder does not exist. Something must have gone wrong at an earlier stage. Check the log and fix possible problem before restarting?" % tree.submodels_path)
                exit(1)

            # Merge point clouds
            if args.merge in ['all', 'pointcloud']:
                if not io.file_exists(tree.odm_georeferencing_model_laz) or self.rerun():
                    all_point_clouds = get_submodel_paths(tree.submodels_path, "odm_georeferencing", "odm_georeferenced_model.laz")
                    
                    try:
                        # pdal.merge_point_clouds(all_point_clouds, tree.odm_georeferencing_model_laz, args.verbose)
                        entwine.build(all_point_clouds, tree.entwine_pointcloud, max_concurrency=args.max_concurrency, rerun=self.rerun())
                    except Exception as e:
                        log.ODM_WARNING("Could not merge point cloud: %s (skipping)" % str(e))
                
                    if io.dir_exists(tree.entwine_pointcloud):
                        try:
                            system.run('pdal translate "ept://{}" "{}"'.format(tree.entwine_pointcloud, tree.odm_georeferencing_model_laz))
                        except Exception as e:
                            log.ODM_WARNING("Cannot export EPT dataset to LAZ: %s" % str(e))
                else:
                    log.ODM_WARNING("Found merged point cloud in %s" % tree.odm_georeferencing_model_laz)
            
            self.update_progress(25)

            # Merge crop bounds
            merged_bounds_file = os.path.join(tree.odm_georeferencing, 'odm_georeferenced_model.bounds.gpkg')
            if not io.file_exists(merged_bounds_file) or self.rerun():
                all_bounds = get_submodel_paths(tree.submodels_path, 'odm_georeferencing', 'odm_georeferenced_model.bounds.gpkg')
                log.ODM_INFO("Merging all crop bounds: %s" % all_bounds)
                if len(all_bounds) > 0:
                    # Calculate a new crop area
                    # based on the convex hull of all crop areas of all submodels
                    # (without a buffer, otherwise we are double-cropping)
                    Cropper.merge_bounds(all_bounds, merged_bounds_file, 0)
                else:
                    log.ODM_WARNING("No bounds found for any submodel.")

            # Merge orthophotos
            if args.merge in ['all', 'orthophoto']:
                if not io.dir_exists(tree.odm_orthophoto):
                    system.mkdir_p(tree.odm_orthophoto)

                if not io.file_exists(tree.odm_orthophoto_tif) or self.rerun():
                    all_orthos_and_cutlines = get_all_submodel_paths(tree.submodels_path,
                        os.path.join("odm_orthophoto", "odm_orthophoto.tif"),
                        os.path.join("odm_orthophoto", "cutline.gpkg"),
                    )

                    if len(all_orthos_and_cutlines) > 1:
                        log.ODM_INFO("Found %s submodels with valid orthophotos and cutlines" % len(all_orthos_and_cutlines))
                        
                        # TODO: histogram matching via rasterio
                        # currently parts have different color tones

                        merged_geotiff = os.path.join(tree.odm_orthophoto, "odm_orthophoto.merged.tif")

                        kwargs = {
                            'orthophoto_merged': merged_geotiff,
                            'input_files': ' '.join(map(lambda i: quote(i[0]), all_orthos_and_cutlines)),
                            'max_memory': get_max_memory(),
                            'threads': args.max_concurrency,
                        }

                        # use bounds as cutlines (blending)
                        if io.file_exists(merged_geotiff):
                            os.remove(merged_geotiff)

                        system.run('gdal_merge.py -o {orthophoto_merged} '
                                #'-createonly '
                                '-co "BIGTIFF=YES" '
                                '-co "BLOCKXSIZE=512" '
                                '-co "BLOCKYSIZE=512" '
                                '--config GDAL_CACHEMAX {max_memory}% '
                                '{input_files} '.format(**kwargs)
                                )

                        for ortho_cutline in all_orthos_and_cutlines:
                            kwargs['input_file'], kwargs['cutline'] = ortho_cutline

                            # Note: cblend has a high performance penalty
                            system.run('gdalwarp -cutline {cutline} '
                                    '-cblend 20 '
                                    '-r bilinear -multi '
                                    '-wo NUM_THREADS={threads} '
                                    '--config GDAL_CACHEMAX {max_memory}% '
                                    '{input_file} {orthophoto_merged}'.format(**kwargs)
                            )

                        # Apply orthophoto settings (compression, tiling, etc.)
                        orthophoto_vars = orthophoto.get_orthophoto_vars(args)

                        if io.file_exists(tree.odm_orthophoto_tif):
                            os.remove(tree.odm_orthophoto_tif)

                        kwargs = {
                            'vars': ' '.join(['-co %s=%s' % (k, orthophoto_vars[k]) for k in orthophoto_vars]),
                            'max_memory': get_max_memory(),
                            'merged': merged_geotiff,
                            'log': tree.odm_orthophoto_tif_log,
                            'orthophoto': tree.odm_orthophoto_tif,
                        }

                        system.run('gdal_translate '
                            '{vars} '
                            '--config GDAL_CACHEMAX {max_memory}% '
                            '{merged} {orthophoto} > {log}'.format(**kwargs))

                        os.remove(merged_geotiff)

                        # Crop
                        if args.crop > 0:
                            Cropper.crop(merged_bounds_file, tree.odm_orthophoto_tif, orthophoto_vars)

                        # Overviews
                        if args.build_overviews:
                            orthophoto.build_overviews(tree.odm_orthophoto_tif) 
                        
                    elif len(all_orthos_and_cutlines) == 1:
                        # Simply copy
                        log.ODM_WARNING("A single orthophoto/cutline pair was found between all submodels.")
                        shutil.copyfile(all_orthos_and_cutlines[0][0], tree.odm_orthophoto_tif)
                    else:
                        log.ODM_WARNING("No orthophoto/cutline pairs were found in any of the submodels. No orthophoto will be generated.")
                else:
                    log.ODM_WARNING("Found merged orthophoto in %s" % tree.odm_orthophoto_tif)

            self.update_progress(75)

            # Merge DEMs
            def merge_dems(dem_filename, human_name):
                if not io.dir_exists(tree.path('odm_dem')):
                    system.mkdir_p(tree.path('odm_dem'))

                dem_file = tree.path("odm_dem", dem_filename)
                if not io.file_exists(dem_file) or self.rerun():
                    all_dems = get_submodel_paths(tree.submodels_path, "odm_dem", dem_filename)
                    log.ODM_INFO("Merging %ss" % human_name)
                    
                    # Merge
                    dem_vars = utils.get_dem_vars(args)
                    euclidean_merge_dems(all_dems, dem_file, dem_vars)

                    if io.file_exists(dem_file):
                        # Crop
                        if args.crop > 0:
                            Cropper.crop(merged_bounds_file, dem_file, dem_vars)
                        log.ODM_INFO("Created %s" % dem_file)
                    else:
                        log.ODM_WARNING("Cannot merge %s, %s was not created" % (human_name, dem_file))
                else:
                    log.ODM_WARNING("Found merged %s in %s" % (human_name, dsm_file))

            if args.merge in ['all', 'dem'] and args.dsm:
                merge_dems("dsm.tif", "DSM")

            if args.merge in ['all', 'dem'] and args.dtm:
                merge_dems("dtm.tif", "DTM")

            # Stop the pipeline short! We're done.
            self.next_stage = None
        else:
            log.ODM_INFO("Normal dataset, nothing to merge.")
            self.progress = 0.0

        