cmake_minimum_required(VERSION 3.20)
project(MusicEngine LANGUAGES CXX)

add_executable(ritmuri_engine ritmuri_engine.cpp)
target_compile_features(ritmuri_engine PRIVATE cxx_std_17)

add_executable(rhythm_search_engine rhythm_search_engine.cpp)
target_compile_features(rhythm_search_engine PRIVATE cxx_std_17)

add_executable(omr_constraint_solver omr_constraint_solver.cpp)
target_compile_features(omr_constraint_solver PRIVATE cxx_std_17)

add_executable(pitch_context_engine pitch_context_engine.cpp)
target_compile_features(pitch_context_engine PRIVATE cxx_std_17)

if(CMAKE_CXX_COMPILER_ID MATCHES "Clang|GNU")
    target_compile_options(ritmuri_engine PRIVATE -Wall -Wextra -Wpedantic)
    target_compile_options(rhythm_search_engine PRIVATE -Wall -Wextra -Wpedantic)
    target_compile_options(omr_constraint_solver PRIVATE -Wall -Wextra -Wpedantic)
    target_compile_options(pitch_context_engine PRIVATE -Wall -Wextra -Wpedantic)
endif()
