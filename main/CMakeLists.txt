idf_component_register(SRCS
                       "main.cpp"
                       "csi_capture.cpp"
                       "wifi_roles.cpp"
                       "udp_bridge.cpp"
                       INCLUDE_DIRS ".")

if(DEFINED CSI_NODE_ID)
    target_compile_definitions(${COMPONENT_LIB} PRIVATE CSI_NODE_ID=${CSI_NODE_ID})
    message(STATUS "CSI_NODE_ID=${CSI_NODE_ID}")
endif()

if(DEFINED CSI_LISTEN_CHANNEL)
    target_compile_definitions(${COMPONENT_LIB} PRIVATE CSI_LISTEN_CHANNEL=${CSI_LISTEN_CHANNEL})
    message(STATUS "CSI_LISTEN_CHANNEL=${CSI_LISTEN_CHANNEL}")
endif()

if(DEFINED CSI_ROLE)
    target_compile_definitions(${COMPONENT_LIB} PRIVATE CSI_ROLE=${CSI_ROLE})
    message(STATUS "CSI_ROLE=${CSI_ROLE} (0=legacy 1=remote 2=agg)")
endif()

if(DEFINED CSI_UDP_PORT)
    target_compile_definitions(${COMPONENT_LIB} PRIVATE CSI_UDP_PORT=${CSI_UDP_PORT})
    message(STATUS "CSI_UDP_PORT=${CSI_UDP_PORT}")
endif()

if(DEFINED CSI_SOFTAP_SSID)
    target_compile_definitions(${COMPONENT_LIB} PRIVATE CSI_SOFTAP_SSID=\"${CSI_SOFTAP_SSID}\")
    message(STATUS "CSI_SOFTAP_SSID=${CSI_SOFTAP_SSID}")
endif()

if(DEFINED CSI_SOFTAP_PASS)
    target_compile_definitions(${COMPONENT_LIB} PRIVATE CSI_SOFTAP_PASS=\"${CSI_SOFTAP_PASS}\")
    message(STATUS "CSI_SOFTAP_PASS=(set)")
endif()
