import ecto

from opendm import log
from opendm import io
from opendm import system
from opendm import context

class ODMOpenSfMCell(ecto.Cell):

    def declare_params(self, params):
        params.declare("use_exif_size", "The application arguments.", False)
        params.declare("feature_process_size", "The application arguments.", False)
        params.declare("feature_min_frames", "The application arguments.", 0)
        params.declare("processes", "The application arguments.", 0)
        params.declare("matching_gps_neighbors", "The application arguments.", 0)

    def declare_io(self, params, inputs, outputs):
        inputs.declare("args", "The application arguments.", {})
        inputs.declare("photos", "Clusters output. list of ODMPhoto's", [])
        outputs.declare("reconstructions", "Clusters output. list of reconstructions", [])

    def process(self, inputs, outputs):

        log.ODM_INFO('Running OMD OpenSfm Cell')

        # get inputs
        args = self.inputs.args
        photos = self.inputs.photos
        project_path = io.absolute_path_file(args['project_path'])

        if not photos:
            log.ODM_ERROR('Not enough photos in photos array to start OpenSfm')
            return ecto.QUIT

        # create opensfm working directory
        opensfm_path = io.join_paths(project_path, 'opensfm')
        system.mkdir_p(opensfm_path)

        # check if reconstruction was done before
        reconstruction_file = io.join_paths(opensfm_path, 'reconstruction.json')

        if io.file_exists(reconstruction_file):
            log.ODM_WARNING('Found a valid reconstruction file in: %s' % 
                (reconstruction_file))
            log.ODM_INFO('Running OMD OpenSfm Cell - Finished')
            return ecto.OK if args['end_with'] != 'opensfm' else ecto.QUIT

        # create file list
        list_path = io.join_paths(opensfm_path, 'image_list.txt')
        with open(list_path, 'w') as fout:
            for photo in photos:
                fout.write('%s\n' % photo.path_file)

        # create config file for OpenSfM
        config = [
            "use_exif_size: %s" % ('no' if not self.params.use_exif_size else 'yes'),
            "feature_process_size: %s" % self.params.feature_process_size,
            "feature_min_frames: %s" % self.params.feature_min_frames,
            "processes: %s" % self.params.processes,
            "matching_gps_neighbors: %s" % self.params.matching_gps_neighbors
        ]

        # write config file
        config_filename = io.join_paths(project_path, 'config.yaml')
        with open(config_filename, 'w') as fout:
            fout.write("\n".join(config))

        # Run OpenSfM reconstruction
        system.run('PYTHONPATH=%s %s/bin/run_all %s' % 
            (context.pyopencv_path, context.opensfm_path, opensfm_path))

        # append reconstructions to output
        self.outputs.reconstructions = []
        print args['end_with']
        
        log.ODM_INFO('Running OMD OpenSfm Cell - Finished')
        return ecto.OK if args['end_with'] != 'opensfm' else ecto.QUIT
 

#########################################################################################


class ODMLoadReconstructionCell(ecto.Cell):    

    def declare_io(self, params, inputs, outputs):
        inputs.declare("project_path", "The directory to the images to load.", "")
        inputs.declare("photos", "Clusters output. list of ODMPhoto's", [])
        outputs.declare("reconstructions", "Clusters output. list of reconstructions", [])

    def process(self, inputs, outputs):        
        log.ODM_INFO('Running OMD Load Reconstruction Cell')
        #log.ODM_WARNING('TODO: Load Reconstruction to as OO')
        log.ODM_INFO('Running OMD Load Reconstruction Cell - Finished')


#########################################################################################


class ODMConvertToPMVSCell(ecto.Cell):    

    def declare_io(self, params, inputs, outputs):
        inputs.declare("project_path", "The directory to the images to load.", "")
        inputs.declare("reconstructions", "The directory to the images to load.", "")
        outputs.declare("reconstruction_path", "The directory to the images to load.", "")

    def process(self, inputs, outputs):

        log.ODM_INFO('Running OMD Convert to PMVS Cell')

        # get inputs
        reconstructions = self.inputs.reconstructions
        project_path = io.absolute_path_file(self.inputs.project_path)

        # define projects location
        pmvs_path = io.join_paths(project_path, 'pmvs')
        opensfm_path = io.join_paths(project_path, 'opensfm')
        reconstruction_path = io.join_paths(pmvs_path, 'recon0')
        pmvs_options_path =  io.join_paths(reconstruction_path, 'pmvs_options.txt')

        # appends created project path to output
        self.outputs.reconstruction_path = reconstruction_path

        if io.file_exists(pmvs_options_path):
            log.ODM_WARNING('Found a valid pmvs options file')
            log.ODM_INFO('Running OMD Convert to PMVS Cell - Finished')
            return ecto.OK
        
        # run converter
        system.run('PYTHONPATH=%s %s/bin/export_pmvs %s --output %s' % 
            (context.pyopencv_path, context.opensfm_path, opensfm_path, pmvs_path))
        
        if io.file_exists(pmvs_options_path):
            log.ODM_DEBUG('PMVS options file created to: %s' % pmvs_options_path)
        else:
            log.ODM_ERROR('Something went wrong when exporting to PMVS')
            return

        log.ODM_INFO('Running OMD Convert to PMVS Cell - Finished')

