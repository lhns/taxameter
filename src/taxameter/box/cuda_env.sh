# shellcheck shell=bash
# Source this before running GPU jobs on a Vast box: makes sure the CUDA driver library initialises.
#
# Some images ship a CUDA forward-compatibility driver library (/usr/local/cuda-*/compat/libcuda.so, registered in
# ldconfig ahead of the host's). On hosts whose kernel driver is older (e.g. 560 with CUDA 12.6) and a consumer GPU,
# that library fails with error 804 ("forward compatibility was attempted on non supported HW") and CUDA programs
# silently fall back to the CPU. Preloading the host's own libcuda fixes it.
#
# The probe calls cuInit through ctypes (stdlib only, no torch): error 804 surfaces exactly there. Without python3 it
# can only check that nvidia-smi sees a GPU, which is weaker; box.sh provision installs python3 when missing.
#
# Sets CUDA_ENV_STATUS to ok | ok_preload | fail.
_cuda_ok() {
  if python3 -c 'import ctypes' > /dev/null 2>&1; then
    python3 -c "import ctypes, sys; sys.exit(0 if ctypes.CDLL('libcuda.so.1').cuInit(0) == 0 else 1)" > /dev/null 2>&1
  else
    nvidia-smi -L > /dev/null 2>&1
  fi
}
if _cuda_ok; then
  CUDA_ENV_STATUS=ok
else
  for lib in /usr/lib/x86_64-linux-gnu/libcuda.so.1 /usr/lib64/libcuda.so.1; do
    if [ -e "$lib" ] && LD_PRELOAD="$lib" _cuda_ok; then
      export LD_PRELOAD="$lib"
      CUDA_ENV_STATUS=ok_preload
      break
    fi
  done
  CUDA_ENV_STATUS=${CUDA_ENV_STATUS:-fail}
fi
export CUDA_ENV_STATUS
