import os
import shutil
from pipes import quote
from opendm import io
from opendm import log
from opendm import system
from opendm import concurrency


def build(input_point_cloud_files, output_path, max_concurrency=8, rerun=False):
    num_files = len(input_point_cloud_files)
    if num_files == 0:
        log.ODM_WARNING("No input point cloud files to process")
        return

    tmpdir = io.related_file_path(output_path, postfix="-tmp")

    def dir_cleanup():
        if io.dir_exists(output_path):
            log.ODM_WARNING("Removing previous EPT directory: %s" % output_path)
            shutil.rmtree(output_path)
    
        if io.dir_exists(tmpdir):
            log.ODM_WARNING("Removing previous EPT temp directory: %s" % tmpdir)
            shutil.rmtree(tmpdir)
    
    if rerun:
        dir_cleanup()

    # Attempt with entwine (faster, more memory hungry)
    try:
        build_entwine(input_point_cloud_files, tmpdir, output_path, max_concurrency=max_concurrency)
    except Exception as e:
        log.ODM_WARNING("Cannot build EPT using entwine (%s), attempting with untwine..." % str(e))
        dir_cleanup()
        build_untwine(input_point_cloud_files, tmpdir, output_path, max_concurrency=max_concurrency)
        
    if os.path.exists(tmpdir):
        shutil.rmtree(tmpdir)


def build_entwine(input_point_cloud_files, tmpdir, output_path, max_concurrency=8):
    kwargs = {
        'threads': max_concurrency,
        'tmpdir': tmpdir,
        'all_inputs': "-i " + " ".join(map(quote, input_point_cloud_files)),
        'outputdir': output_path
    }

    # Run scan to compute dataset bounds
    system.run('entwine scan --threads {threads} --tmp "{tmpdir}" {all_inputs} -o "{outputdir}"'.format(**kwargs))
    scan_json = os.path.join(output_path, "scan.json")

    if os.path.exists(scan_json):
        kwargs['input'] = scan_json
        for _ in range(len(input_point_cloud_files)):
            # One at a time
            system.run('entwine build --threads {threads} --tmp "{tmpdir}" -i "{input}" -o "{outputdir}" --run 1'.format(**kwargs))
    else:
        log.ODM_WARNING("%s does not exist, no point cloud will be built." % scan_json)
        

def build_untwine(input_point_cloud_files, tmpdir, output_path, max_concurrency=8, rerun=False):
    kwargs = {
        # 'threads': max_concurrency,
        'tmpdir': tmpdir,
        'files': "--files " + " ".join(map(quote, input_point_cloud_files)),
        'outputdir': output_path
    }

    # Run untwine
    system.run('untwine --temp_dir "{tmpdir}" {files} --output_dir "{outputdir}"'.format(**kwargs))
