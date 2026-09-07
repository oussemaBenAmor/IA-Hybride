package com.brms.odm.exception;

public class CaseNotSupportedException extends RuntimeException {
    public CaseNotSupportedException(String caseName) {
        super("Cas non supporté : '" + caseName + "'");
    }
}
