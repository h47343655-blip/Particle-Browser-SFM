# Particle preview renderer: Linux .so (tests) and Windows DLLs (SFM).
#   make            build everything available
#   make linux      build/libpp_render.so
#   make windows    ../game/.../bin/pp_render_win32.dll and pp_render_win64.dll
#   make asan       build/libpp_render_asan.so (address + undefined behaviour sanitizers)

SRC := pp_api.c pp_defs.c pp_sim.c pp_vtf.c pp_raster.c
HDR := pp_render.h pp_internal.h
BIN := ../game/usermod/scripts/sfm/sfm_particle_browser/bin
CWARN := -std=c99 -Wall -Wextra -Wno-unused-parameter -Wshadow -Wpointer-arith
# -O2 is fixed: an environment CFLAGS (e.g. "-g0") must not silently drop optimisation.
COMMON := $(CWARN) -O2 $(CFLAGS) -fno-strict-aliasing -ffile-prefix-map=$(CURDIR)=pp

MINGW32 := i686-w64-mingw32-gcc
MINGW64 := x86_64-w64-mingw32-gcc
WINFLAGS := -shared -static -static-libgcc -s -Wl,--no-insert-timestamp -Wl,--enable-stdcall-fixup
# SSE2 math for speed; realign the stack because Windows only guarantees 4 bytes on x86.
WIN32FLAGS := -msse2 -mfpmath=sse -mstackrealign

.PHONY: all linux windows asan clean

all: linux windows

linux: build/libpp_render.so
windows: $(BIN)/pp_render_win32.dll $(BIN)/pp_render_win64.dll
asan: build/libpp_render_asan.so

build/libpp_render.so: $(SRC) $(HDR)
	@mkdir -p build
	gcc $(COMMON) -fPIC -shared -fvisibility=hidden -o $@ $(SRC) -lm

build/libpp_render_asan.so: $(SRC) $(HDR)
	@mkdir -p build
	gcc $(CWARN) -O1 -g -fPIC -shared -fvisibility=hidden -fsanitize=address,undefined -fno-omit-frame-pointer \
		-fno-sanitize-recover=undefined -o $@ $(SRC) -lm

$(BIN)/pp_render_win32.dll: $(SRC) $(HDR)
	@mkdir -p $(BIN)
	$(MINGW32) $(COMMON) $(WIN32FLAGS) $(WINFLAGS) -o $@ $(SRC) -lm

$(BIN)/pp_render_win64.dll: $(SRC) $(HDR)
	@mkdir -p $(BIN)
	$(MINGW64) $(COMMON) $(WINFLAGS) -o $@ $(SRC) -lm

clean:
	rm -rf build
