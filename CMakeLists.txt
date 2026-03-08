cmake_minimum_required(VERSION 3.20)
project(FuckDX LANGUAGES CXX)

set(CMAKE_CXX_STANDARD 20)

include(FetchContent)
FetchContent_Declare(
    minhook
    GIT_REPOSITORY https://github.com/TsudaKageyu/minhook
    GIT_TAG master
)
FetchContent_Declare(
    libhat
    GIT_REPOSITORY https://github.com/BasedInc/libhat.git
    GIT_TAG v0.9.0
)
FetchContent_MakeAvailable(minhook libhat)

add_library(FuckDX SHARED src/main.cpp)
target_link_libraries(FuckDX PRIVATE minhook libhat::libhat)
