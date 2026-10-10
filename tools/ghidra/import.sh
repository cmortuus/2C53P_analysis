#!/bin/zsh
# Create/refresh the Ghidra project for the 2C53P APP image.
#   tools/ghidra/import.sh            # fresh import + analysis + labels
#   tools/ghidra/import.sh -process   # re-apply Labels2C53P on the existing project (no analysis)
#   tools/ghidra/import.sh -check     # print key symbols to confirm labels are present
# Open afterwards with:  ghidraRun  -> File > Open Project -> ghidra/2C53P.gpr
set -e
ROOT="${0:A:h:h:h}"
GH="$(brew --prefix ghidra)/libexec/support/analyzeHeadless"
PROJ="$ROOT/ghidra"; mkdir -p "$PROJ"
IMG="$ROOT/2C53P_Firmware_V1.14/2C53P_APP_V1.14_250925.bin"
PROG="2C53P_APP_V1.14_250925.bin"
export FNIRSI_ROOT="$ROOT"
export JAVA_HOME="$(brew --prefix openjdk@21)/libexec/openjdk.jdk/Contents/Home"; export PATH="$JAVA_HOME/bin:$PATH"
SP=(-scriptPath "$ROOT/tools/ghidra")
case "$1" in
  -check)
    "$GH" "$PROJ" 2C53P -process "$PROG" -noanalysis $SP -postScript Check2C53P.java 2>&1 | grep 'CHECK' ;;
  -seed)
    "$GH" "$PROJ" 2C53P -process "$PROG" $SP -preScript Seed2C53P.java -postScript Labels2C53P.java \
         -analysisTimeoutPerFile 1800 2>&1 | grep -E 'Seed2C53P|Labels2C53P|ERROR|Exception|Total Time' ;;
  -decomp)
    shift; "$GH" "$PROJ" 2C53P -process "$PROG" -noanalysis -readOnly $SP -postScript Decomp2C53P.java "$@" 2>&1 \
      | grep -E 'DECOMP|ERROR|Exception' ;;
  -process)
    "$GH" "$PROJ" 2C53P -process "$PROG" -noanalysis $SP -postScript Labels2C53P.java 2>&1 \
      | grep -E 'Labels2C53P|ERROR|Exception' ;;
  *)
    "$GH" "$PROJ" 2C53P -import "$IMG" -overwrite \
         -processor ARM:LE:32:Cortex -cspec default \
         -loader BinaryLoader -loader-baseAddr 0xE000 -loader-blockName APP_FLASH \
         $SP -preScript Setup2C53P.java -postScript Labels2C53P.java \
         -analysisTimeoutPerFile 1800 ;;
esac
