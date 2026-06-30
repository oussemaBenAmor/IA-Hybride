package com.brms.odm.config;

import lombok.Data;
import org.springframework.boot.context.properties.ConfigurationProperties;

@Data
@ConfigurationProperties(prefix = "odm")
public class OdmProperties {
    private String baseUrl;
    private String username;
    private String password;

    // chemins relatifs à baseUrl, un par opération de décision
    private String creditImmobilierPath;
    private String creditConsommationPath;
    private String virementPath;
    private String assurancePath;
    private String cartePath;
}