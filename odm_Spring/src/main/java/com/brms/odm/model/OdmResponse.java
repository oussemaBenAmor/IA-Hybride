package com.brms.odm.model;

import lombok.Builder;
import lombok.Data;
import java.util.List;
import java.util.Map;

@Data
@Builder
public class OdmResponse {
    private String decision;
    private List<String> rulesTriggered;
    private Map<String, Object> details;
}