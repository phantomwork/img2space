set(_proj_name opencv)
set(_SB_BINARY_DIR "${SB_BINARY_DIR}/${_proj_name}")

ExternalProject_Add(${_proj_name}
  PREFIX            ${_SB_BINARY_DIR}
  TMP_DIR           ${_SB_BINARY_DIR}/tmp
  STAMP_DIR         ${_SB_BINARY_DIR}/stamp
  #--Download step--------------
  DOWNLOAD_DIR      ${SB_DOWNLOAD_DIR}
  URL               https://github.com/opencv/opencv/archive/4.5.0.zip
  #--Update/Patch step----------
  UPDATE_COMMAND    ""
  #--Configure step-------------
  SOURCE_DIR        ${SB_SOURCE_DIR}/${_proj_name}
  CMAKE_ARGS
    -DBUILD_opencv_core=ON
    -DBUILD_opencv_imgproc=ON
    -DBUILD_opencv_highgui=ON
    -DBUILD_opencv_video=ON
    -DBUILD_opencv_ml=ON
    -DBUILD_opencv_features2d=ON
    -DBUILD_opencv_calib3d=ON
    -DBUILD_opencv_contrib=ON
    -DBUILD_opencv_flann=ON
    -DBUILD_opencv_objdetect=ON
    -DBUILD_opencv_photo=ON
    -DBUILD_opencv_legacy=ON
    -DBUILD_opencv_python=ON
    -DWITH_FFMPEG=${ODM_BUILD_SLAM}
    -DWITH_CUDA=OFF
    -DWITH_GTK=${ODM_BUILD_SLAM}
    -DWITH_VTK=OFF
    -DWITH_EIGEN=OFF
    -DWITH_OPENNI=OFF
    -DBUILD_EXAMPLES=OFF
    -DBUILD_TESTS=OFF
    -DBUILD_PERF_TESTS=OFF
    -DBUILD_DOCS=OFF
    -DBUILD_opencv_apps=OFF
    -DBUILD_opencv_gpu=OFF
    -DBUILD_opencv_videostab=OFF
    -DBUILD_opencv_nonfree=OFF
    -DBUILD_opencv_stitching=OFF
    -DBUILD_opencv_world=OFF
    -DBUILD_opencv_superres=OFF
    -DBUILD_opencv_java=OFF
    -DBUILD_opencv_ocl=OFF
    -DBUILD_opencv_ts=OFF
    -DBUILD_opencv_xfeatures2d=ON
    -DOPENCV_ALLOCATOR_STATS_COUNTER_TYPE=int64_t
    -DCMAKE_BUILD_TYPE:STRING=Release
    -DCMAKE_INSTALL_PREFIX:PATH=${SB_INSTALL_DIR}
  #--Build step-----------------
  BINARY_DIR        ${_SB_BINARY_DIR}
  #--Install step---------------
  INSTALL_DIR       ${SB_INSTALL_DIR}
  #--Output logging-------------
  LOG_DOWNLOAD      OFF
  LOG_CONFIGURE     OFF
  LOG_BUILD         OFF
)
