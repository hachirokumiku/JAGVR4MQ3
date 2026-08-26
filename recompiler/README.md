# Jaguar AOT Recompiler Framework

This directory contains a prototype framework to analyze an Atari Jaguar ROM (mc.rom) and generate C skeletons for ahead-of-time (AOT) native compilation on Windows. This is a tooling scaffold — not a complete production recompiler — designed to help you iteratively collect hot basic blocks from a ROM, generate C stubs for those blocks, and produce a native DLL that a Python launcher can load.

Important notes
- I will NOT include or distribute any ROM data or libretro core binaries. You must place your own mc.rom and (optionally) virtualjaguar_libretro.dll in the repository root before running the tools.
- The generated C is a skeleton framework that maps out basic blocks and provides function templates. Turning those templates into a fully correct, fast native recompiler requires deeper M68k/Jaguar MMIO handling and manual work. This framework accelerates that workflow.
- Licensing: virtualjaguar-libretro is GPLv3. If your workflow uses that DLL for dynamic tracing, you must comply with the GPL when distributing the DLL or derivatives.

Quick workflow
1. Place mc.rom next to the repository root.
2. Run the static analyzer to extract basic blocks:
   python scripts/recompile_trace.py --rom mc.rom --out trace.json
3. Generate C skeletons from the trace:
   python generators/generate_c_blocks.py --trace trace.json --out out/recompiled_game.c
4. Build the runtime into a DLL (see builders/ for examples). On Windows with MinGW:
   gcc -shared -o out/recompiled_game.dll out/recompiled_game.c runtime/runtime.c -O3
5. Use launcher/launcher.py to load the DLL and run tests.

See the scripts for command-line flags and options.
