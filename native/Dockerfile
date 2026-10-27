# CUDA 13 no longer publishes Ubuntu 20.04 images. Its Rocky Linux 8 toolkit
# runs on glibc 2.28; copy only the toolkit, never its system libraries/sysroot.
ARG CUDA13_IMAGE=nvidia/cuda:13.1.1-devel-rockylinux8@sha256:06343f198491f2edabfd6e8755ad459a98173ad4bb1f756250fa4f187bf2b2ec
ARG CUDA12_IMAGE=nvidia/cuda:12.4.1-devel-ubuntu20.04@sha256:8d577fd078ae56c37493af4454a5b700c72a7f1aeb9ff3d0adc0459fc47752a4
ARG UBUNTU_IMAGE=ubuntu:20.04@sha256:8feb4d8ca5354def3d8fce243717141ce31e2c428701f6682bd2fafe15388214
FROM ${CUDA13_IMAGE} AS cuda13-toolkit
FROM ${CUDA12_IMAGE} AS cuda12-toolkit

FROM ${UBUNTU_IMAGE} AS cpu
ARG DEBIAN_FRONTEND=noninteractive
ARG RUST_VERSION=1.98.0
RUN apt-get update && apt-get install --yes --no-install-recommends \
    binutils build-essential ca-certificates curl git libclang-dev libtinfo6 \
    ninja-build patchelf pkg-config python3 python3-pip \
    && rm -rf /var/lib/apt/lists/*
# Focal's CMake 3.16 predates CUDA 13 support. The pinned wheels support glibc
# 2.17 or older on both release architectures and do not replace the sysroot.
COPY native/build-requirements.txt /tmp/build-requirements.txt
RUN python3 -m pip install --no-cache-dir --no-deps --require-hashes -r /tmp/build-requirements.txt
RUN curl --proto '=https' --tlsv1.2 --silent --show-error --fail https://sh.rustup.rs \
    | sh -s -- -y --profile minimal --default-toolchain "${RUST_VERSION}" --no-modify-path
ENV PATH=/root/.cargo/bin:${PATH}
RUN test "$(getconf GNU_LIBC_VERSION)" = "glibc 2.31" \
    && git config --global --add safe.directory /work
WORKDIR /work

FROM cpu AS cuda-builder
# CPU dispatch variants include AVX-VNNI/BF16/AMX, beyond Focal's GCC 9.
# Clang 16 supports those instructions and both CUDA compilers while using
# Focal's original libstdc++ headers/runtime and glibc sysroot.
RUN curl --fail --silent --show-error https://apt.llvm.org/llvm-snapshot.gpg.key -o /usr/share/keyrings/llvm.asc \
    && echo '8b2a587ffd672c4687e7581dad4b2f6c1bb2ad6b480cd9771ba2ff48e0b8c75d  /usr/share/keyrings/llvm.asc' | sha256sum -c - \
    && echo 'deb [signed-by=/usr/share/keyrings/llvm.asc] https://apt.llvm.org/focal/ llvm-toolchain-focal-16 main' > /etc/apt/sources.list.d/llvm.list \
    && apt-get update && apt-get install --yes --no-install-recommends clang-16 \
    && rm -rf /var/lib/apt/lists/*
ENV CC=clang-16 CXX=clang++-16 CUDAHOSTCXX=clang++-16

FROM cuda-builder AS cuda12
COPY --from=cuda12-toolkit /usr/local/cuda-12.4 /usr/local/cuda-12.4
RUN ln -s cuda-12.4 /usr/local/cuda
ENV PATH=/usr/local/cuda/bin:${PATH}
RUN nvcc --version

FROM cuda-builder AS cuda13
COPY --from=cuda13-toolkit /usr/local/cuda-13.1 /usr/local/cuda-13.1
RUN ln -s cuda-13.1 /usr/local/cuda
ENV PATH=/usr/local/cuda/bin:${PATH}
RUN nvcc --version
