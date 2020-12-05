import math
import re
import cv2
import os
from opendm import dls
import numpy as np
from opendm import log
from opensfm.io import imread

from skimage import exposure
from skimage.morphology import disk
from skimage.filters import rank, gaussian

# Loosely based on https://github.com/micasense/imageprocessing/blob/master/micasense/utils.py

def dn_to_radiance(photo, image):
    """
    Convert Digital Number values to Radiance values
    :param photo ODM_Photo
    :param image numpy array containing image data
    :return numpy array with radiance image values
    """

    image = image.astype("float32")
    if len(image.shape) != 3:
        raise ValueError("Image should have shape length of 3 (got: %s)" % len(image.shape))

    # Handle thermal bands (experimental)
    if photo.band_name == 'LWIR':
        image -= (273.15 * 100.0) # Convert Kelvin to Celsius
        image *= 0.01
        return image
    
    # All others
    a1, a2, a3 = photo.get_radiometric_calibration()
    dark_level = photo.get_dark_level()

    exposure_time = photo.exposure_time
    gain = photo.get_gain()
    photometric_exp = photo.get_photometric_exposure()

    if a1 is None and photometric_exp is None:
        log.ODM_WARNING("Cannot perform radiometric calibration, no FNumber/Exposure Time or Radiometric Calibration EXIF tags found in %s. Using Digital Number." % photo.filename)
        return image
    
    if a1 is None and photometric_exp is not None:
        a1 = photometric_exp

    V, x, y = vignette_map(photo)
    if x is None:
        x, y = np.meshgrid(np.arange(photo.width), np.arange(photo.height))

    if dark_level is not None:
        image -= dark_level

    # Normalize DN to 0 - 1.0
    bit_depth_max = photo.get_bit_depth_max()
    if bit_depth_max:
        image /= bit_depth_max

    if V is not None:
        # vignette correction
        V = np.repeat(V[:, :, np.newaxis], image.shape[2], axis=2)
        image *= V

    if exposure_time and a2 is not None and a3 is not None:
        # row gradient correction
        R = 1.0 / (1.0 + a2 * y / exposure_time - a3 * y)
        R = np.repeat(R[:, :, np.newaxis], image.shape[2], axis=2)
        image *= R
    
    # Floor any negative radiances to zero (can happend due to noise around blackLevel)
    if dark_level is not None:
        image[image < 0] = 0
    
    # apply the radiometric calibration - i.e. scale by the gain-exposure product and
    # multiply with the radiometric calibration coefficient

    if gain is not None and exposure_time is not None:
        image /= (gain * exposure_time)
    
    image *= a1

    return image

def vignette_map(photo):
    x_vc, y_vc = photo.get_vignetting_center()
    polynomial = photo.get_vignetting_polynomial()

    if x_vc and polynomial:
        # append 1., so that we can call with numpy polyval
        polynomial.append(1.0)
        vignette_poly = np.array(polynomial)

        # perform vignette correction
        # get coordinate grid across image
        x, y = np.meshgrid(np.arange(photo.width), np.arange(photo.height))

        # meshgrid returns transposed arrays
        # x = x.T
        # y = y.T

        # compute matrix of distances from image center
        r = np.hypot((x - x_vc), (y - y_vc))

        # compute the vignette polynomial for each distance - we divide by the polynomial so that the
        # corrected image is image_corrected = image_original * vignetteCorrection

        vignette = 1.0 / np.polyval(vignette_poly, r)
        return vignette, x, y
    
    return None, None, None

def dn_to_reflectance(photo, image, use_sun_sensor=True):
    radiance = dn_to_radiance(photo, image)
    irradiance = compute_irradiance(photo, use_sun_sensor=use_sun_sensor)
    return radiance * math.pi / irradiance

def compute_irradiance(photo, use_sun_sensor=True):
    # Thermal?
    if photo.band_name == "LWIR":
        return 1.0

    # Some cameras (Micasense) store the value (nice! just return)
    hirradiance = photo.get_horizontal_irradiance()
    if hirradiance is not None:
        return hirradiance

    # TODO: support for calibration panels

    if use_sun_sensor and photo.get_sun_sensor():
        # Estimate it
        dls_orientation_vector = np.array([0,0,-1])
        sun_vector_ned, sensor_vector_ned, sun_sensor_angle, \
        solar_elevation, solar_azimuth = dls.compute_sun_angle([photo.latitude, photo.longitude],
                                        photo.get_dls_pose(),
                                        photo.get_utc_time(),
                                        dls_orientation_vector)

        angular_correction = dls.fresnel(sun_sensor_angle)

        # TODO: support for direct and scattered irradiance

        direct_to_diffuse_ratio = 6.0 # Assumption, clear skies
        spectral_irradiance = photo.get_sun_sensor()

        percent_diffuse = 1.0 / direct_to_diffuse_ratio
        sensor_irradiance = spectral_irradiance / angular_correction

        # Find direct irradiance in the plane normal to the sun
        untilted_direct_irr = sensor_irradiance / (percent_diffuse + np.cos(sun_sensor_angle))
        direct_irradiance = untilted_direct_irr
        scattered_irradiance = untilted_direct_irr * percent_diffuse

        # compute irradiance on the ground using the solar altitude angle
        horizontal_irradiance = direct_irradiance * np.sin(solar_elevation) + scattered_irradiance
        return horizontal_irradiance
    elif use_sun_sensor:
        log.ODM_WARNING("No sun sensor values found for %s" % photo.filename)
    
    return 1.0

def get_photos_by_band(multi_camera, user_band_name):
    band_name = get_primary_band_name(multi_camera, user_band_name)

    for band in multi_camera:
        if band['name'] == band_name:
            return band['photos']


def get_primary_band_name(multi_camera, user_band_name):
    if len(multi_camera) < 1:
        raise Exception("Invalid multi_camera list")
    
    # multi_camera is already sorted by band_index
    if user_band_name == "auto":
        return multi_camera[0]['name']

    for band in multi_camera:
        if band['name'].lower() == user_band_name.lower():
            return band['name']
    
    band_name_fallback = multi_camera[0]['name']

    log.ODM_WARNING("Cannot find band name \"%s\", will use \"%s\" instead" % (user_band_name, band_name_fallback))
    return band_name_fallback


def compute_band_maps(multi_camera, primary_band):
    """
    Computes maps of: 
     - { photo filename --> associated primary band photo } (s2p)
     - { primary band filename --> list of associated secondary band photos } (p2s)
    by looking at capture time or filenames as a fallback
    """
    band_name = get_primary_band_name(multi_camera, primary_band)
    primary_band_photos = None
    for band in multi_camera:
        if band['name'] == band_name:
            primary_band_photos = band['photos']
            break
    
    # Try using capture time as the grouping factor
    try:
        capture_time_map = {}
        s2p = {}
        p2s = {}

        for p in primary_band_photos:
            t = p.get_utc_time()
            if t is None:
                raise Exception("Cannot use capture time (no information in %s)" % p.filename)
            
            # Should be unique across primary band
            if capture_time_map.get(t) is not None:
                raise Exception("Unreliable capture time detected (duplicate)")

            capture_time_map[t] = p
        
        for band in multi_camera:
            photos = band['photos']

            for p in photos:
                t = p.get_utc_time()
                if t is None:
                    raise Exception("Cannot use capture time (no information in %s)" % p.filename)
                
                # Should match the primary band
                if capture_time_map.get(t) is None:
                    raise Exception("Unreliable capture time detected (no primary band match)")

                s2p[p.filename] = capture_time_map[t]

                if band['name'] != band_name:
                    p2s.setdefault(capture_time_map[t].filename, []).append(p)

        return s2p, p2s
    except Exception as e:
        # Fallback on filename conventions
        log.ODM_WARNING("%s, will use filenames instead" % str(e))

        filename_map = {}
        s2p = {}
        p2s = {}
        file_regex = re.compile(r"^(.+)[-_]\w+(\.[A-Za-z]{3,4})$")

        for p in primary_band_photos:
            filename_without_band = re.sub(file_regex, "\\1\\2", p.filename)

            # Quick check
            if filename_without_band == p.filename:
                raise Exception("Cannot match bands by filename on %s, make sure to name your files [filename]_band[.ext] uniformly." % p.filename)

            filename_map[filename_without_band] = p

        for band in multi_camera:
            photos = band['photos']

            for p in photos:
                filename_without_band = re.sub(file_regex, "\\1\\2", p.filename)

                # Quick check
                if filename_without_band == p.filename:
                    raise Exception("Cannot match bands by filename on %s, make sure to name your files [filename]_band[.ext] uniformly." % p.filename)

                s2p[p.filename] = filename_map[filename_without_band]

                if band['name'] != band_name:
                    p2s.setdefault(filename_map[filename_without_band].filename, []).append(p)

        return s2p, p2s

def compute_alignment_matrices(multi_camera, primary_band_name, images_path, s2p, p2s, max_samples=9999):
    log.ODM_INFO("Computing band alignment")

    alignment_info = {}

    # For each secondary band
    for band in multi_camera:
        if band['name'] != primary_band_name:
            matrices = []

            # if band['name'] != "NIR":
            #     continue # TODO REMOVE

            # Find good matrix candidates for alignment
            for p in band['photos']:
                primary_band_photo = s2p.get(p.filename)
                if primary_band_photo is None:
                    log.ODM_WARNING("Cannot find primary band photo for %s" % p.filename)
                    continue
                
                warp_matrix, score, dimension = compute_homography(os.path.join(images_path, p.filename),
                                                            os.path.join(images_path, primary_band_photo.filename))

                if warp_matrix is not None:
                    log.ODM_INFO("%s --> %s good match (score: %s)" % (p.filename, primary_band_photo.filename, score))
                    matrices.append({
                        'warp_matrix': warp_matrix,
                        'score': score,
                        'dimension': dimension
                    })
                else:
                    log.ODM_INFO("%s --> %s cannot be matched" % (p.filename, primary_band_photo.filename))
                    
                if len(matrices) >= max_samples:
                    log.ODM_INFO("Got enough samples for %s (%s)" % (band['name'], max_samples))
                    break
            
            # Sort
            matrices.sort(key=lambda x: x['score'], reverse=False)
            
            if len(matrices) > 0:
                alignment_info[band['name']] = matrices[0]
                print(matrices[0])
            else:
                log.ODM_WARNING("Cannot find alignment matrix for band %s, The band will likely be misaligned!" % band['name'])

    return alignment_info

def compute_homography(image_filename, align_image_filename):
    # try:
    # Convert images to grayscale if needed
    image = imread(image_filename, unchanged=True, anydepth=True)
    if image.shape[2] == 3:
        image_gray = to_8bit(cv2.cvtColor(image, cv2.COLOR_BGR2GRAY))
    else:
        image_gray = to_8bit(image[:,:,0])

    align_image = imread(align_image_filename, unchanged=True, anydepth=True)
    if align_image.shape[2] == 3:
        align_image_gray = to_8bit(cv2.cvtColor(align_image, cv2.COLOR_BGR2GRAY))
    else:
        align_image_gray = to_8bit(align_image[:,:,0])

    def compute_using(algorithm):
        h = algorithm(image_gray, align_image_gray)
        if h is None:
            return None, None, (None, None)

        det = np.linalg.det(h)
        
        # Check #1 homography's determinant will not be close to zero
        if abs(det) < 0.25:
            return None, None, (None, None)

        # Check #2 the ratio of the first-to-last singular value is sane (not too high)
        svd = np.linalg.svd(h, compute_uv=False)
        if svd[-1] == 0:
            return None, None, (None, None)
        
        ratio = svd[0] / svd[-1]
        if ratio > 100000:
            return None, None, (None, None)

        return h, compute_alignment_score(h, image_gray, align_image_gray), (align_image_gray.shape[1], align_image_gray.shape[0])
    
    result = compute_using(find_features_homography)
    if result[0] is None:
        log.ODM_INFO("Can't use features matching, will use ECC")
        result = compute_using(find_ecc_homography)
    
    return result

    # except Exception as e:
    #     log.ODM_WARNING("Compute homography: %s" % str(e))
    #     return None, None, (None, None)

def find_ecc_homography(image_gray, align_image_gray, number_of_iterations=5000, termination_eps=1e-8):
    image_gray = to_8bit(gradient(gaussian(image_gray)))
    align_image_gray = to_8bit(gradient(gaussian(align_image_gray)))

    # Define the motion model
    warp_matrix = np.eye(3, 3, dtype=np.float32)

    # Define termination criteria
    criteria = (cv2.TERM_CRITERIA_EPS | cv2.TERM_CRITERIA_COUNT,
     number_of_iterations, termination_eps)

    try:
      (cc, warp_matrix) = cv2.findTransformECC (image_gray,align_image_gray,warp_matrix, cv2.MOTION_HOMOGRAPHY, criteria, inputMask=None, gaussFiltSize=1)
    except:
      (cc, warp_matrix) = cv2.findTransformECC (image_gray,align_image_gray,warp_matrix, cv2.MOTION_HOMOGRAPHY, criteria)

    return warp_matrix
    

def find_features_homography(image_gray, align_image_gray, feature_retention=0.25):
    # Detect SIFT features and compute descriptors.
    detector = cv2.SIFT_create(edgeThreshold=10, contrastThreshold=0.1)
    kp_image, desc_image = detector.detectAndCompute(image_gray, None)
    kp_align_image, desc_align_image = detector.detectAndCompute(align_image_gray, None)

    # Match
    bf = cv2.BFMatcher(cv2.NORM_L1,crossCheck=True)
    matches = bf.match(desc_image, desc_align_image)

    # Sort by score
    matches.sort(key=lambda x: x.distance, reverse=False)

    # Remove bad matches
    num_good_matches = int(len(matches) * feature_retention)
    matches = matches[:num_good_matches]

    # Debug
    # imMatches = cv2.drawMatches(im1, kp_image, im2, kp_align_image, matches, None)
    # cv2.imwrite("matches.jpg", imMatches)

    # Extract location of good matches
    points_image = np.zeros((len(matches), 2), dtype=np.float32)
    points_align_image = np.zeros((len(matches), 2), dtype=np.float32)

    for i, match in enumerate(matches):
        points_image[i, :] = kp_image[match.queryIdx].pt
        points_align_image[i, :] = kp_align_image[match.trainIdx].pt

    # Find homography
    h, _ = cv2.findHomography(points_image, points_align_image, cv2.RANSAC)
    return h

def compute_alignment_score(warp_matrix, image_gray, align_image_gray, apply_gradient=True):
    projected = align_image(image_gray, warp_matrix, (align_image_gray.shape[1], align_image_gray.shape[0]))
    borders = projected==0
    
    if apply_gradient:
        image_gray = to_8bit(gradient(gaussian(image_gray)))
        align_image_gray = to_8bit(gradient(gaussian(align_image_gray)))
    
    # cv2.imwrite("/datasets/micasense/opensfm/undistorted/align_image_gray.jpg", align_image_gray)
    # cv2.imwrite("/datasets/micasense/opensfm/undistorted/projected.jpg", projected)
    
    # Threshold
    align_image_gray[align_image_gray > 128] = 255
    projected[projected > 128] = 255
    align_image_gray[align_image_gray <= 128] = 0
    projected[projected <= 128] = 0

    # Mark borders
    align_image_gray[borders] = 0
    projected[borders] = 255
    

    # cv2.imwrite("/datasets/micasense/opensfm/undistorted/threshold_align_image_gray.jpg", align_image_gray)
    # cv2.imwrite("/datasets/micasense/opensfm/undistorted/threshold_projected.jpg", projected)
    
    # cv2.imwrite("/datasets/micasense/opensfm/undistorted/delta.jpg", projected - align_image_gray)

    # Compute delta --> the more the images overlap perfectly, the lower the score
    return (projected - align_image_gray).sum()


def gradient(im, ksize=5):
    im = local_normalize(im)
    grad_x = cv2.Sobel(im,cv2.CV_32F,1,0,ksize=ksize)
    grad_y = cv2.Sobel(im,cv2.CV_32F,0,1,ksize=ksize)
    grad = cv2.addWeighted(np.absolute(grad_x), 0.5, np.absolute(grad_y), 0.5, 0)
    return grad

def local_normalize(im):
    width, _ = im.shape
    disksize = int(width/5)
    if disksize % 2 == 0:
        disksize = disksize + 1
    selem = disk(disksize)
    im = rank.equalize(im, selem=selem)
    return im


def align_image(image, warp_matrix, dimension):
    if warp_matrix.shape == (3, 3):
        return cv2.warpPerspective(image, warp_matrix, dimension)
    else:
        return cv2.warpAffine(image, warp_matrix, dimension)


def to_8bit(image):
    if image.dtype == np.uint8:
        return image

    # Convert to 8bit
    try:
        data_range = np.iinfo(image.dtype)
        value_range = float(data_range.max) - float(data_range.min)
    except ValueError:
        # For floats use the actual range of the image values
        value_range = float(image.max()) - float(image.min())
    
    image = image.astype(np.float32)
    image *= 255.0 / value_range
    np.around(image, out=image)
    image[image > 255] = 255
    image[image < 0] = 0
    image = image.astype(np.uint8)

    return image


