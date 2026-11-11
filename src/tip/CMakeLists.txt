cmake_minimum_required(VERSION 3.20)
project(oyayubi_tip LANGUAGES CXX)

set(CMAKE_MSVC_RUNTIME_LIBRARY "MultiThreaded")
if(WIN32)
  add_library(oyayubi_tip SHARED
    oyayubi_tip.cpp
    tip_json.h
    oyayubi_tip.def
  )

  target_compile_features(oyayubi_tip PRIVATE cxx_std_17)
  target_compile_definitions(oyayubi_tip PRIVATE
    UNICODE _UNICODE WIN32_LEAN_AND_MEAN WINVER=0x0A00 _WIN32_WINNT=0x0A00)
  if(MSVC)
    target_compile_options(oyayubi_tip PRIVATE /utf-8)
  endif()
  target_link_libraries(oyayubi_tip PRIVATE ole32 oleaut32 uuid advapi32 user32 gdi32 ws2_32)
  set_target_properties(oyayubi_tip PROPERTIES
    OUTPUT_NAME NicolaIME3
    PREFIX ""
    RUNTIME_OUTPUT_DIRECTORY "${CMAKE_SOURCE_DIR}/../../dist"
    LIBRARY_OUTPUT_DIRECTORY "${CMAKE_SOURCE_DIR}/../../dist"
  )
endif()

# Also buildable on Linux: this executable has no Win32/TSF dependencies.
option(TIP_BUILD_JSON_TESTS "Build standalone JSON parser checks" ON)
if(TIP_BUILD_JSON_TESTS)
  enable_testing()
  add_executable(tip_json_test tests/tip_json_test.cpp tip_json.h)
  target_compile_features(tip_json_test PRIVATE cxx_std_17)
  # Assertions are the checks, including in Release builds.
  if(MSVC)
    target_compile_options(tip_json_test PRIVATE /UNDEBUG)
  else()
    target_compile_options(tip_json_test PRIVATE -UNDEBUG)
  endif()
  add_test(NAME tip_json COMMAND tip_json_test)
endif()
