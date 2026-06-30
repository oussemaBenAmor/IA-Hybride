package com.brms.odm.model;

import lombok.Data;
import java.util.Map;

@Data
public class OdmRequest {
    private String caseName;
    private Map<String, Object> params;
}