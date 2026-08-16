cmake_minimum_required(VERSION 3.20)
project(oyayubi_tip LANGUAGES CXX)

add_library(oyayubi_tip SHARED
  oyayubi_tip.cpp
  oyayubi_tip.def
)

target_compile_features(oyayubi_tip PRIVATE cxx_std_17)
target_compile_definitions(oyayubi_tip PRIVATE UNICODE _UNICODE WIN32_LEAN_AND_MEAN)
set(CMAKE_MSVC_RUNTIME_LIBRARY "MultiThreaded")
if(MSVC)
  target_compile_options(oyayubi_tip PRIVATE /MT)
endif()
target_link_libraries(oyayubi_tip PRIVATE ole32 oleaut32 uuid advapi32)
set_target_properties(oyayubi_tip PROPERTIES
  OUTPUT_NAME NicolaIME3
  PREFIX ""
  RUNTIME_OUTPUT_DIRECTORY "${CMAKE_SOURCE_DIR}/../../dist"
  LIBRARY_OUTPUT_DIRECTORY "${CMAKE_SOURCE_DIR}/../../dist"
)
