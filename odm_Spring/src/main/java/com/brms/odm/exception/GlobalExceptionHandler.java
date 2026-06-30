package com.brms.odm.exception;

import com.brms.odm.model.ApiError;
import org.springframework.http.HttpStatus;
import org.springframework.web.bind.annotation.ExceptionHandler;
import org.springframework.web.bind.annotation.ResponseStatus;
import org.springframework.web.bind.annotation.RestControllerAdvice;
import org.springframework.web.client.RestClientException;

@RestControllerAdvice
public class GlobalExceptionHandler {

    @ExceptionHandler(CaseNotSupportedException.class)
    @ResponseStatus(HttpStatus.UNPROCESSABLE_ENTITY) // 422
    public ApiError handleUnsupported(CaseNotSupportedException ex) {
        return new ApiError("CASE_NOT_SUPPORTED", ex.getMessage());
    }

    @ExceptionHandler(RestClientException.class)
    @ResponseStatus(HttpStatus.BAD_GATEWAY) // 502
    public ApiError handleOdmUnavailable(RestClientException ex) {
        return new ApiError("ODM_UNAVAILABLE",
                "Impossible de joindre le service de décision ODM : " + ex.getMessage());
    }

    @ExceptionHandler(Exception.class)
    @ResponseStatus(HttpStatus.INTERNAL_SERVER_ERROR) // 500
    public ApiError handleOther(Exception ex) {
        return new ApiError("INTERNAL_ERROR", ex.getMessage());
    }
}