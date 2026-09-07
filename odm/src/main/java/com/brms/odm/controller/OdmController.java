package com.brms.odm.controller;
import com.brms.odm.model.OdmRequest;
import com.brms.odm.model.OdmResponse;
import com.brms.odm.service.OdmService;
import lombok.RequiredArgsConstructor;
import org.springframework.http.ResponseEntity;
import org.springframework.web.bind.annotation.*;

@RestController
@RequestMapping("/api/odm")
@RequiredArgsConstructor
public class OdmController {

    private final OdmService odmService;

    @PostMapping("/execute")
    public ResponseEntity<OdmResponse> execute(@RequestBody OdmRequest request) {
        return ResponseEntity.ok(odmService.execute(request));
    }

    @GetMapping("/health")
    public ResponseEntity<String> health() {
        return ResponseEntity.ok("ODM gateway is running");
    }
}