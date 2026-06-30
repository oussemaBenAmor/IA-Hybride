package com.brms.odm.config;

import lombok.RequiredArgsConstructor;
import org.springframework.context.annotation.Bean;
import org.springframework.context.annotation.Configuration;
import org.springframework.web.client.RestClient;

@Configuration
@RequiredArgsConstructor
public class RestClientConfig {

    private final OdmProperties props;

    @Bean
    RestClient odm() {
        return RestClient.builder()
                .baseUrl(props.getBaseUrl())
                .defaultHeaders(h -> h.setBasicAuth(props.getUsername(), props.getPassword()))
                .build();
    }
}
