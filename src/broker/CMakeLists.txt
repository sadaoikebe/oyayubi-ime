cmake_minimum_required(VERSION 3.20)
project(oyayubi_broker LANGUAGES CXX)

# The tag this build belongs to (REDESIGN §4.2): the broker only runs when
# HKCU\Software\oyayubi-ime\Tag names it. Release builds pass -DOYAYUBI_TAG=<tag>.
set(OYAYUBI_TAG "dev" CACHE STRING "the version tag compiled into the broker")

set(CMAKE_MSVC_RUNTIME_LIBRARY "MultiThreaded")
set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)

add_library(broker_core STATIC broker_core.cpp broker_core.h support.cpp support.h)
if(MSVC)
  target_compile_options(broker_core PUBLIC /utf-8 /W4)
else()
  target_compile_options(broker_core PUBLIC -Wall -Wextra)
endif()

if(WIN32)
  add_executable(oyayubi-broker WIN32 platform_win.cpp broker.rc)
  target_compile_definitions(oyayubi-broker PRIVATE
    UNICODE _UNICODE WIN32_LEAN_AND_MEAN NOMINMAX WINVER=0x0A00 _WIN32_WINNT=0x0A00
    OYAYUBI_TAG="${OYAYUBI_TAG}")
  target_link_libraries(oyayubi-broker PRIVATE broker_core
    advapi32 userenv shell32 user32 comctl32 ole32 uuid)
  if(MINGW)  # wWinMain (MSVC finds it by itself); used to compile-check on Linux
    target_link_options(oyayubi-broker PRIVATE -municode)
  endif()
  # The generator expression keeps multi-config generators from adding Release/.
  set_target_properties(oyayubi-broker PROPERTIES
    RUNTIME_OUTPUT_DIRECTORY "$<1:${CMAKE_SOURCE_DIR}/../../dist>")
else()
  # The same broker on POSIX, for the tests (platform_posix.cpp).
  add_executable(oyayubi-broker platform_posix.cpp)
  target_link_libraries(oyayubi-broker PRIVATE broker_core)
endif()

enable_testing()
add_executable(broker_core_test tests/broker_core_test.cpp)
target_link_libraries(broker_core_test PRIVATE broker_core)
if(MSVC)
  target_compile_options(broker_core_test PRIVATE /UNDEBUG)
else()
  target_compile_options(broker_core_test PRIVATE -UNDEBUG)
endif()
add_test(NAME broker_core COMMAND broker_core_test)
