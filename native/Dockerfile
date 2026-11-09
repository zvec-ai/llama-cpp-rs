# Keep the compiler/sysroot on glibc 2.28 for both Linux architectures.
# CUDA contributes only its toolkit, never the donor image's system libraries.
ARG TARGETARCH
ARG MANYLINUX_X64_IMAGE=quay.io/pypa/manylinux_2_28_x86_64:2026.03.01-1@sha256:d5f19b5957cf25df7ac15465924cafe2358754a187e510af386bf9596598a372
ARG MANYLINUX_ARM64_IMAGE=quay.io/pypa/manylinux_2_28_aarch64:2026.03.01-1@sha256:34368babc008d6405b4b74c77868d6a5d15b6010cb953574d1f2bb9184392a9b
ARG RUNTIME_X64_IMAGE=almalinux:8.10@sha256:1965cb10f9c392766b2d544d3c1b997eb008d0e413c808e6a20086e87c802a80
ARG RUNTIME_ARM64_IMAGE=almalinux:8.10@sha256:29fdedeed8a6707558c9e03b5d3e8f09eac5307cdd0811218d7d55a2cc9ae522
ARG CUDA13_IMAGE=nvidia/cuda:13.1.1-devel-rockylinux8@sha256:06343f198491f2edabfd6e8755ad459a98173ad4bb1f756250fa4f187bf2b2ec
ARG CUDA12_IMAGE=nvidia/cuda:12.4.1-devel-ubuntu20.04@sha256:8d577fd078ae56c37493af4454a5b700c72a7f1aeb9ff3d0adc0459fc47752a4
FROM ${CUDA13_IMAGE} AS cuda13-toolkit
FROM ${CUDA12_IMAGE} AS cuda12-toolkit
FROM ${MANYLINUX_X64_IMAGE} AS builder-amd64
FROM ${MANYLINUX_ARM64_IMAGE} AS builder-arm64
FROM ${RUNTIME_X64_IMAGE} AS runtime-amd64
FROM ${RUNTIME_ARM64_IMAGE} AS runtime-arm64

FROM builder-${TARGETARCH} AS cpu
ARG RUST_VERSION=1.98.0
# GCC 13 supports the existing CPU dispatch variants and both CUDA compilers.
# Its EL8 compatibility libraries retain the system libstdc++ ABI; do not copy
# a newer distribution's libstdc++ into the SDK or runtime images.
RUN dnf install --assumeyes --setopt=install_weak_deps=False \
        gcc-toolset-13-gcc-c++ clang-libs ninja-build pkgconf-pkg-config \
    && dnf clean all
ENV PATH=/root/.cargo/bin:/opt/rh/gcc-toolset-13/root/usr/bin:/opt/python/cp312-cp312/bin:/usr/local/bin:/usr/bin:/bin
ENV CC=gcc CXX=g++ CUDAHOSTCXX=g++ LIBCLANG_PATH=/usr/lib64
COPY native/build-requirements.txt /tmp/build-requirements.txt
RUN python3 -m pip install --no-cache-dir --no-deps --require-hashes -r /tmp/build-requirements.txt
RUN curl --proto '=https' --tlsv1.2 --silent --show-error --fail https://sh.rustup.rs \
    | sh -s -- -y --profile minimal --default-toolchain "${RUST_VERSION}" --no-modify-path
RUN test "$(getconf GNU_LIBC_VERSION)" = "glibc 2.28" \
    && test "$(gcc -dumpversion)" = 13 \
    && git config --global --add safe.directory /work
WORKDIR /work

FROM cpu AS cuda12
COPY --from=cuda12-toolkit /usr/local/cuda-12.4 /usr/local/cuda-12.4
RUN ln -s cuda-12.4 /usr/local/cuda
ENV PATH=/usr/local/cuda/bin:${PATH}
RUN nvcc --version

FROM cpu AS cuda13
COPY --from=cuda13-toolkit /usr/local/cuda-13.1 /usr/local/cuda-13.1
RUN ln -s cuda-13.1 /usr/local/cuda
ENV PATH=/usr/local/cuda/bin:${PATH}
RUN nvcc --version

# Run acceptance tests against the system runtime, without the build toolset.
FROM runtime-${TARGETARCH} AS runtime-cpu
RUN dnf install --assumeyes --setopt=install_weak_deps=False \
        ca-certificates curl libstdc++ python3.12 \
    && dnf clean all \
    && ln -s /usr/bin/python3.12 /usr/local/bin/python3 \
    && test "$(getconf GNU_LIBC_VERSION)" = "glibc 2.28"
WORKDIR /work

FROM runtime-cpu AS runtime-cuda12
COPY --from=cuda12-toolkit /usr/local/cuda-12.4/targets/x86_64-linux/lib/libcudart.so* /usr/local/cuda/lib64/
COPY --from=cuda12-toolkit /usr/local/cuda-12.4/targets/x86_64-linux/lib/libcublas.so* /usr/local/cuda/lib64/
COPY --from=cuda12-toolkit /usr/local/cuda-12.4/targets/x86_64-linux/lib/libcublasLt.so* /usr/local/cuda/lib64/
ENV LD_LIBRARY_PATH=/usr/local/cuda/lib64:/usr/local/nvidia/lib:/usr/local/nvidia/lib64
ENV NVIDIA_VISIBLE_DEVICES=all NVIDIA_DRIVER_CAPABILITIES=compute,utility

FROM runtime-cpu AS runtime-cuda13
COPY --from=cuda13-toolkit /usr/local/cuda-13.1/targets/x86_64-linux/lib/libcudart.so* /usr/local/cuda/lib64/
COPY --from=cuda13-toolkit /usr/local/cuda-13.1/targets/x86_64-linux/lib/libcublas.so* /usr/local/cuda/lib64/
COPY --from=cuda13-toolkit /usr/local/cuda-13.1/targets/x86_64-linux/lib/libcublasLt.so* /usr/local/cuda/lib64/
ENV LD_LIBRARY_PATH=/usr/local/cuda/lib64:/usr/local/nvidia/lib:/usr/local/nvidia/lib64
ENV NVIDIA_VISIBLE_DEVICES=all NVIDIA_DRIVER_CAPABILITIES=compute,utility
