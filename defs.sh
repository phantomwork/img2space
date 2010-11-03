
echo sdg

set -o nounset
set -o errexit

if [ ! -n "$0" ]; then
	TOOLS_PATH=$(dirname $(which $0));
	IMAGE_DIR="."
else
	TOOLS_PATH=$PWD
fi

TOOLS_BIN_PATH=$TOOLS_PATH/bin
TOOLS_INC_PATH=$TOOLS_PATH/include
TOOLS_LIB_PATH=$TOOLS_PATH/lib
TOOLS_SRC_PATH=$TOOLS_PATH/src
TOOLS_LOG_PATH=$TOOLS_PATH/logs

LIB_PATH="/usr/local/lib"
INC_PATH="/usr/local/include"

BUNDLER_PATH="$TOOLS_SRC_PATH/bundler"
CMVS_PATH="$TOOLS_SRC_PATH/cmvs"
PMVS_PATH="$TOOLS_SRC_PATH/pmvs"
GRACLUS_PATH="$TOOLS_SRC_PATH/graclus"
CLAPACK_PATH="$TOOLS_SRC_PATH/clapack"
OPENCV_PATH="$TOOLS_SRC_PATH/openCv"
VLFEAT_PATH="$TOOLS_SRC_PATH/vlfeat"