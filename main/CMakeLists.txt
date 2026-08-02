idf_component_register(SRCS "main.cpp"
                    INCLUDE_DIRS ".")

if(DEFINED CSI_NODE_ID)
    target_compile_definitions(${COMPONENT_LIB} PRIVATE CSI_NODE_ID=${CSI_NODE_ID})
    message(STATUS "CSI_NODE_ID=${CSI_NODE_ID}")
endif()
