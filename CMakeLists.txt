cmake_minimum_required(VERSION 3.26)
project(tirtc_python LANGUAGES C)

if(NOT DEFINED TIRTC_RUNTIME_ROOT)
  message(FATAL_ERROR "TIRTC_RUNTIME_ROOT must point to one Runtime tuple root")
endif()

set(TIRTC_INCLUDE_DIR "${TIRTC_RUNTIME_ROOT}/include")
set(TIRTC_LIBRARY_DIR "${TIRTC_RUNTIME_ROOT}/lib")
set(TIRTC_SHARE_DIR "${TIRTC_RUNTIME_ROOT}/share")

if(APPLE)
  set(CMAKE_OSX_DEPLOYMENT_TARGET "11.5" CACHE STRING "" FORCE)
  set(TIRTC_RUNTIME_LIBRARY "${TIRTC_LIBRARY_DIR}/libtirtc_media.dylib")
  set(TIRTC_INSTALL_RPATH "@loader_path/.libs")
else()
  set(TIRTC_RUNTIME_LIBRARY "${TIRTC_LIBRARY_DIR}/libtirtc_media.so")
  set(TIRTC_INSTALL_RPATH "$ORIGIN/.libs")
endif()

foreach(path IN ITEMS
    "${TIRTC_INCLUDE_DIR}/ti/error.h"
    "${TIRTC_INCLUDE_DIR}/ti/media.h"
    "${TIRTC_INCLUDE_DIR}/ti/rtc.h"
    "${TIRTC_INCLUDE_DIR}/ti/runtime.h"
    "${TIRTC_INCLUDE_DIR}/ti/storage.h"
    "${TIRTC_RUNTIME_LIBRARY}")
  if(NOT EXISTS "${path}")
    message(FATAL_ERROR "Runtime input is incomplete: ${path}")
  endif()
endforeach()

find_package(Python3 3.11 REQUIRED COMPONENTS Development.SABIModule)
Python3_add_library(_native MODULE USE_SABI 3.11 WITH_SOABI native/extension.c)
target_compile_features(_native PRIVATE c_std_11)
target_include_directories(_native PRIVATE "${TIRTC_INCLUDE_DIR}")
target_link_libraries(_native PRIVATE "${TIRTC_RUNTIME_LIBRARY}")
set_target_properties(_native PROPERTIES
  BUILD_RPATH "${TIRTC_LIBRARY_DIR}"
  INSTALL_RPATH "${TIRTC_INSTALL_RPATH}"
  INSTALL_RPATH_USE_LINK_PATH FALSE
)

install(TARGETS _native LIBRARY DESTINATION tirtc)
install(DIRECTORY "${TIRTC_LIBRARY_DIR}/" DESTINATION tirtc/.libs
  FILES_MATCHING PATTERN "*.dylib" PATTERN "*.so")
if(EXISTS "${TIRTC_SHARE_DIR}/licenses")
  install(DIRECTORY "${TIRTC_SHARE_DIR}/licenses/" DESTINATION tirtc/licenses)
endif()
