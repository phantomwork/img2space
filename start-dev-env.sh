#!/bin/bash
set -eo pipefail
__dirname=$(cd $(dirname "$0"); pwd -P)
cd "${__dirname}"

if [ "$1" = "--setup" ]; then
    export HOME=/home/$2

    if [ ! -f .setupdevenv ]; then
        echo "Recompiling environment... this might take a while."
        #bash configure.sh reinstall
        
        touch .setupdevenv
        apt install -y vim
        chown -R $3:$4 /code /var/www
    fi

    echo "echo '' && echo '' && echo '' && echo '###################################' && echo 'ODM Dev Environment Ready. Hack on!' && echo '###################################' && echo '' && cd /code" > $HOME/.bashrc

    # Install qt creator
    if hash qtcreator 2>/dev/null; then
        has_qtcreator="YES"
    fi

    if [ "$has_qtcreator" != "YES" ] && [ "$5" == "YES" ]; then 
        apt install -y libxrender1 gdb qtcreator
    fi

    # Install liquidprompt
    if [ ! -e "$HOME/liquidprompt" ]; then
        git clone https://github.com/nojhan/liquidprompt.git --depth 1 $HOME/liquidprompt
    fi
    
    if [ -e "$HOME/liquidprompt" ]; then
        echo "export LP_PS1_PREFIX='(odmdev)'" >> $HOME/.bashrc
        echo "source $HOME/liquidprompt/liquidprompt" >> $HOME/.bashrc
    fi

    # Colors
    echo "alias ls='ls --color=auto'" >> $HOME/.bashrc

    su -c bash $2 
    exit 0
fi

platform="Linux" # Assumed
uname=$(uname)
case $uname in
	"Darwin")
	platform="MacOS / OSX"
	;;
	MINGW*)
	platform="Windows"
	;;
esac

if [[ $platform != "Linux" ]]; then
	echo "This script only works on Linux."
    exit 1
fi

if hash docker 2>/dev/null; then
    has_docker="YES"
fi

if [ "$has_docker" != "YES" ]; then
    echo "You need to install docker before running this script."
    exit 1
fi

export PORT="${PORT:=3000}"
export QTC="${QTC:=NO}"

if [ -z "$DATA" ]; then
    echo "Usage: DATA=/path/to/datasets [VARS] $0"
    echo
    echo "VARS:"
    echo "	DATA	Path to directory that contains datasets for testing. The directory will be mounted in /datasets. If you don't have any, simply set it to a folder outside the ODM repository."
    echo "	PORT	Port to expose for NodeODM (default: $PORT)"
    echo "	QTC	When set to YES, installs QT Creator for C++ development (default: $QTC)"
    exit 1
fi


echo "Starting development environment..."
echo "Datasets path: $DATA"
echo "NodeODM port: $PORT"
echo "QT Creator: $QTC"

if [ ! -e "$HOME"/.odm-dev-home ]; then
    mkdir -p "$HOME"/.odm-dev-home
fi

USER_ID=$(id -u)
GROUP_ID=$(id -g)
USER=$(id -un)
xhost +
docker run -ti --entrypoint bash --name odmdev -v $(pwd):/code -v "$DATA":/datasets -p $PORT:3000 --privileged -e DISPLAY -e LANG=C.UTF-8 -e LC_ALL=C.UTF-8 -v="/etc/passwd:/etc/passwd:ro" -v="/tmp/.X11-unix:/tmp/.X11-unix:rw" -v="$HOME/.odm-dev-home:/home/$USER" opendronemap/nodeodm -c "/code/start-dev-env.sh --setup $USER $USER_ID $GROUP_ID $QTC"
exit 0