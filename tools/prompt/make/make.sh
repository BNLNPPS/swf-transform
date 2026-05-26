#!/bin/bash

CurrentDir="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
ToolsPromptDir="$( dirname "$CurrentDir" )"
ToolsDir="$( dirname "$ToolsPromptDir" )"
RootDir="$( dirname "$ToolsDir" )"

EXECNAME=${RootDir}/bin/run_prompt_wrapper
rm -fr $EXECNAME

workdir=/tmp/idds
tmpzip=/tmp/idds/tmp.zip
rm -fr $workdir
mkdir -p $workdir

echo "setup virtualenv $workdir"

python3.9 -m venv $workdir
source $workdir/bin/activate

echo "install panda client"
pip install panda-client
# pip install requests urllib3==1.26.18 argcomplete packaging anytree networkx stomp.py==8.0.1
# pip install requests urllib3 argcomplete packaging stomp.py wheel cachetools
pip install "requests<2.29" "urllib3<2" argcomplete packaging stomp.py wheel cachetools

echo "install idds-common"
pip install idds-common

echo "install package"
pip install $RootDir

python_lib_path=`python -c 'from sysconfig import get_path; print(get_path("purelib"))'`
echo $python_lib_path

cur_dir=$PWD

# cd ${python_lib_path}
# # for libname in idds pandaclient pandatools tabulate pyjwt requests urllib3 argcomplete cryptography packaging anytree networkx; do
# for libname in idds pandaclient pandatools tabulate jwt requests urllib3 argcomplete cryptography packaging stomp; do
#     echo zip -r $tmpzip  $libname
#     zip -r $tmpzip  $libname
# done
# cd -

cd $workdir
mkdir lib_py
# for libname in idds pandaclient pandatools tabulate pyjwt requests urllib3 argcomplete cryptography packaging anytree networkx; do
# for libname in idds pandaclient pandatools tabulate jwt requests urllib3 argcomplete cryptography packaging stomp cffi charset_normalizer docopt.py idna pycparser six.py websocket _cffi_backend*; do
for libname in idds pandaclient pandatools tabulate requests urllib3 argcomplete stomp websocket charset_normalizer idna certifi packaging cachetools swf_transform; do
    echo cp -fr ${python_lib_path}/$libname lib_py
    cp -fr ${python_lib_path}/$libname lib_py
done
echo zip -r $tmpzip lib_py
zip -r $tmpzip lib_py
cd -

cd $workdir
echo zip -r $tmpzip  etc
zip -r $tmpzip  etc
cd -

cd ${RootDir}
echo zip -r $tmpzip  bin
zip -r $tmpzip  bin

cd ${RootDir}
echo zip -r $tmpzip  conf
zip -r $tmpzip  conf

cd ${RootDir}
echo zip -r $tmpzip  wrapper
zip -r $tmpzip  wrapper

cd -

cat ${RootDir}/tools/prompt/make/zipheader $tmpzip > $EXECNAME
chmod +x $EXECNAME
