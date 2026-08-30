CXX      ?= g++
CXXFLAGS ?= -O3 -march=x86-64-v3 -std=c++17 -Wall

BINDIR   := bin
SRCDIR   := .

SOURCES_SIMPLE  := membound.cpp sleeper.cpp
TARGETS_SIMPLE  := $(SOURCES_SIMPLE:.cpp=)

SOURCES_SIMD    := simd_levels.cpp
TARGETS_SIMD    := simd_levels_scalar simd_levels_sse simd_levels_avx

# avx-512: build only if compiler supports it
AVX512_OK := $(shell $(CXX) $(CXXFLAGS) -mavx512f -mavx512dq -DUSE_AVX512 \
              -fsyntax-only $(SRCDIR)/$(SOURCES_SIMD) 2>/dev/null && echo yes)
ifeq ($(AVX512_OK),yes)
TARGETS_SIMD += simd_levels_avx512
endif

TARGETS := $(TARGETS_SIMPLE) $(TARGETS_SIMD)

.PHONY: all clean

all: $(addprefix $(BINDIR)/,$(TARGETS))

$(BINDIR):
	mkdir -p $(BINDIR)

# simple targets: one source, no extra flags
$(BINDIR)/membound: $(SRCDIR)/membound.cpp | $(BINDIR)
	$(CXX) $(CXXFLAGS) -o $@ $<

$(BINDIR)/sleeper: $(SRCDIR)/sleeper.cpp | $(BINDIR)
	$(CXX) $(CXXFLAGS) -o $@ $<

# simd tiers: same source, different flags + defines
$(BINDIR)/simd_levels_scalar: $(SRCDIR)/simd_levels.cpp | $(BINDIR)
	$(CXX) $(CXXFLAGS) -fno-tree-vectorize -fno-tree-slp-vectorize -DUSE_SCALAR -o $@ $<

$(BINDIR)/simd_levels_sse: $(SRCDIR)/simd_levels.cpp | $(BINDIR)
	$(CXX) $(CXXFLAGS) -msse4.2 -DUSE_SSE -o $@ $<

$(BINDIR)/simd_levels_avx: $(SRCDIR)/simd_levels.cpp | $(BINDIR)
	$(CXX) $(CXXFLAGS) -mavx2 -mfma -DUSE_AVX -o $@ $<

$(BINDIR)/simd_levels_avx512: $(SRCDIR)/simd_levels.cpp | $(BINDIR)
	$(CXX) $(CXXFLAGS) -mavx512f -mavx512dq -DUSE_AVX512 -o $@ $<

clean:
	rm -rf $(BINDIR)
