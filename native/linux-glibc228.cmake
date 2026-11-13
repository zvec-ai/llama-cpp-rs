# The SDK does not build llama-server/tools or MTMD video import. Their subprocess
# helper calls posix_spawn_file_actions_addchdir_np, which requires glibc 2.29.
# Keep the rest of common (including the Rust wrapper) in the glibc 2.28 SDK.
set(LLAMA_SUBPROCESS OFF CACHE BOOL "Subprocess tools are not part of the Linux SDK" FORCE)
