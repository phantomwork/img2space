import shutil, os, glob, math

from opendm import log
from opendm import io
from opendm import system
from opendm import context
from opendm import point_cloud
from opendm import types
from opendm.osfm import OSFMContext

class ODMMveStage(types.ODM_Stage):
    def process(self, args, outputs):
        # get inputs
        tree = outputs['tree']
        reconstruction = outputs['reconstruction']
        photos = reconstruction.photos

        if not photos:
            log.ODM_ERROR('Not enough photos in photos array to start MVE')
            exit(1)

        # check if reconstruction was done before
        if not io.file_exists(tree.mve_model) or self.rerun():
            # cleanup if a rerun
            if io.dir_exists(tree.mve_path) and self.rerun():
                shutil.rmtree(tree.mve_path)

            # make bundle directory
            if not io.file_exists(tree.mve_bundle):
                system.mkdir_p(tree.mve_path)
                system.mkdir_p(io.join_paths(tree.mve_path, 'bundle'))

                octx = OSFMContext(tree.opensfm)
                octx.save_absolute_image_list_to(tree.mve_image_list)
                io.copy(tree.opensfm_bundle, tree.mve_bundle)

            # mve makescene wants the output directory
            # to not exists before executing it (otherwise it
            # will prompt the user for confirmation)
            if io.dir_exists(tree.mve):
                shutil.rmtree(tree.mve)

            # run mve makescene
            if not io.dir_exists(tree.mve_views):
                system.run('%s "%s" "%s"' % (context.makescene_path, tree.mve_path, tree.mve), env_vars={'OMP_NUM_THREADS': args.max_concurrency})

            self.update_progress(10)

            # Compute mve output scale based on depthmap_resolution
            max_width = 0
            max_height = 0
            for photo in photos:
                max_width = max(photo.width, max_width)
                max_height = max(photo.height, max_height)

            max_pixels = args.depthmap_resolution * args.depthmap_resolution
            if max_width * max_height <= max_pixels:
                mve_output_scale = 0
            else:
                ratio = float(max_width * max_height) / float(max_pixels)
                mve_output_scale = int(math.ceil(math.log(ratio) / math.log(4.0)))

            dmrecon_config = [
                "-s%s" % mve_output_scale,
	            "--progress=silent",
                "--local-neighbors=2",
                # "--filter-width=3",
            ]

            # Run MVE's dmrecon
            log.ODM_INFO('                                                                               ')
            log.ODM_INFO('                                    ,*/**                                      ')
            log.ODM_INFO('                                  ,*@%*/@%*                                    ')
            log.ODM_INFO('                                ,/@%******@&*.                                 ')
            log.ODM_INFO('                              ,*@&*********/@&*                                ')
            log.ODM_INFO('                            ,*@&**************@&*                              ')
            log.ODM_INFO('                          ,/@&******************@&*.                           ')
            log.ODM_INFO('                        ,*@&*********************/@&*                          ')
            log.ODM_INFO('                      ,*@&**************************@&*.                       ')
            log.ODM_INFO('                    ,/@&******************************&&*,                     ')
            log.ODM_INFO('                  ,*&&**********************************@&*.                   ')
            log.ODM_INFO('                ,*@&**************************************@&*.                 ')
            log.ODM_INFO('              ,*@&***************#@@@@@@@@@%****************&&*,               ')
            log.ODM_INFO('            .*&&***************&@@@@@@@@@@@@@@****************@@*.             ')
            log.ODM_INFO('          .*@&***************&@@@@@@@@@@@@@@@@@%****(@@%********@@*.           ')
            log.ODM_INFO('        .*@@***************%@@@@@@@@@@@@@@@@@@@@@#****&@@@@%******&@*,         ')
            log.ODM_INFO('      .*&@****************@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@/*****@@*.       ')
            log.ODM_INFO('    .*@@****************@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@%*************@@*.     ')
            log.ODM_INFO('  .*@@****/***********@@@@@&**(@@@@@@@@@@@@@@@@@@@@@@@#*****************%@*,   ')
            log.ODM_INFO(' */@*******@*******#@@@@%*******/@@@@@@@@@@@@@@@@@@@@********************/@(,  ')
            log.ODM_INFO(' ,*@(********&@@@@@@#**************/@@@@@@@#**(@@&/**********************@&*   ')
            log.ODM_INFO('   *#@/*******************************@@@@@***&@&**********************&@*,    ')
            log.ODM_INFO('     *#@#******************************&@@@***@#*********************&@*,      ')
            log.ODM_INFO('       */@#*****************************@@@************************@@*.        ')
            log.ODM_INFO('         *#@/***************************/@@/*********************%@*,          ')
            log.ODM_INFO('           *#@#**************************#@@%******************%@*,            ')
            log.ODM_INFO('             */@#*************************(@@@@@@@&%/********&@*.              ')
            log.ODM_INFO('               *(@(*********************************/%@@%**%@*,                ')
            log.ODM_INFO('                 *(@%************************************%@**                  ')
            log.ODM_INFO('                   **@%********************************&@*,                    ')
            log.ODM_INFO('                     *(@(****************************%@/*                      ')
            log.ODM_INFO('                       ,(@%************************#@/*                        ')
            log.ODM_INFO('                         ,*@%********************&@/,                          ')
            log.ODM_INFO('                           */@#****************#@/*                            ')
            log.ODM_INFO('                             ,/@&************#@/*                              ')
            log.ODM_INFO('                               ,*@&********%@/,                                ')
            log.ODM_INFO('                                 */@#****(@/*                                  ')
            log.ODM_INFO('                                   ,/@@@@(*                                    ')
            log.ODM_INFO('                                     .**,                                      ')
            log.ODM_INFO('')
            log.ODM_INFO("Running dense reconstruction. This might take a while. Please be patient, the process is not dead or hung.")
            log.ODM_INFO("                              Process is running")
            
            # TODO: find out why MVE is crashing at random
            # MVE *seems* to have a race condition, triggered randomly, regardless of dataset
            # https://gist.github.com/pierotofy/6c9ce93194ba510b61e42e3698cfbb89
            # Temporary workaround is to retry the reconstruction until we get it right
            # (up to a certain number of retries).
            retry_count = 1
            while retry_count < 10:
                try:
                    system.run('%s "%s" "%s"' % (context.dmrecon_path, ' '.join(dmrecon_config), tree.mve), env_vars={'OMP_NUM_THREADS': args.max_concurrency})
                    break
                except Exception as e:
                    if str(e) == "Child returned 134" or str(e) == "Child returned 1":
                        retry_count += 1
                        log.ODM_WARNING("Caught error code, retrying attempt #%s" % retry_count)
                    else:
                        raise e

            self.update_progress(90)

            scene2pset_config = [
                "-F%s" % mve_output_scale
            ]

            # run scene2pset
            system.run('%s %s "%s" "%s"' % (context.scene2pset_path, ' '.join(scene2pset_config), tree.mve, tree.mve_model), env_vars={'OMP_NUM_THREADS': args.max_concurrency})
        
            # run cleanmesh (filter points by MVE confidence threshold)
            if args.mve_confidence > 0:
                mve_filtered_model = io.related_file_path(tree.mve_model, postfix=".filtered")
                system.run('%s -t%s --no-clean --component-size=0 "%s" "%s"' % (context.meshclean_path, min(1.0, args.mve_confidence), tree.mve_model, mve_filtered_model), env_vars={'OMP_NUM_THREADS': args.max_concurrency})

                if io.file_exists(mve_filtered_model):
                    os.remove(tree.mve_model)
                    os.rename(mve_filtered_model, tree.mve_model)
                else:
                    log.ODM_WARNING("Couldn't filter MVE model (%s does not exist)." % mve_filtered_model)
        else:
            log.ODM_WARNING('Found a valid MVE reconstruction file in: %s' %
                            tree.mve_model)
