package com.brms.odm.service;

import com.banque.domain.*;
import com.brms.odm.config.OdmProperties;
import com.brms.odm.exception.CaseNotSupportedException;
import com.brms.odm.model.OdmRequest;
import com.brms.odm.model.OdmResponse;
import lombok.RequiredArgsConstructor;
import org.springframework.stereotype.Service;
import org.springframework.web.client.RestClient;

import java.util.HashMap;
import java.util.List;
import java.util.Map;

@Service
@RequiredArgsConstructor
public class OdmService {

    private final RestClient odm;
    private final OdmProperties props;

    public OdmResponse execute(OdmRequest req) {
        String caseName = req.getCaseName() == null ? "" : req.getCaseName();
        Map<String, Object> p = req.getParams() != null ? req.getParams() : Map.of();

        return switch (caseName) {
            case "credit_immobilier"   -> callImmobilier(p);
            case "credit_consommation" -> callConsommation(p);
            case "virement"            -> callVirement(p);
            case "assurance_vie"       -> callAssurance(p);
            case "carte_bancaire"      -> callCarte(p);
            default -> throw new CaseNotSupportedException(caseName);
        };
    }

    // ── Crédit immobilier ─────────────────────────────────────────────
    private OdmResponse callImmobilier(Map<String, Object> p) {
        CreditRequest r = new CreditRequest();
        r.setMontant(toDouble(p.get("montant")));
        r.setRevenuMensuel(toDouble(p.get("revenu_mensuel")));
        r.setDureeMois(toInt(p.get("duree_mois")));
        r.setApport(toDouble(p.get("apport")));
        r.setAge(toInt(p.get("age")));
        return call(props.getCreditImmobilierPath(), "request", r, "response");
    }

    // ── Crédit consommation ───────────────────────────────────────────
    private OdmResponse callConsommation(Map<String, Object> p) {
        CreditRequest r = new CreditRequest();
        r.setMontant(toDouble(p.get("montant")));
        r.setRevenuMensuel(toDouble(p.get("revenu_mensuel")));
        r.setDureeMois(toInt(p.get("duree_mois")));
        r.setApport(toDouble(p.get("apport")));
        r.setAge(toInt(p.get("age")));
        return call(props.getCreditConsommationPath(), "request", r, "response");
    }

    // ── Virement ──────────────────────────────────────────────────────
    private OdmResponse callVirement(Map<String, Object> p) {
        VirementRequest r = new VirementRequest();
        r.setTypeVirement(toStr(p.get("type_virement")) != null ? toStr(p.get("type_virement")) : "standard");
        r.setMontant(toDouble(p.get("montant")));
        r.setIban(toStr(p.get("iban")));
        return call(props.getVirementPath(), "requestVirement", r, "responseVirement");
    }

    // ── Assurance vie ─────────────────────────────────────────────────
    private OdmResponse callAssurance(Map<String, Object> p) {
        AssuranceVieRequest r = new AssuranceVieRequest();
        r.setOperation(toStr(p.get("operation")));
        r.setMontantInitial(toDouble(p.get("montant_initial")));
        r.setAge(toInt(p.get("age")));
        return call(props.getAssurancePath(), "requestAssurance", r, "responseAssurance");
    }

    // ── Carte bancaire ────────────────────────────────────────────────
    private OdmResponse callCarte(Map<String, Object> p) {
        CarteBancaireRequest r = new CarteBancaireRequest();
        r.setOperation(toStr(p.get("operation")));
        double plafond = toDouble(p.get("plafond"));
        if (plafond <= 0) plafond = toDouble(p.get("plafond_souhaite"));
        r.setPlafond(plafond);
        return call(props.getCartePath(), "requestCarte", r, "responseCarte");
    }

    // ── Appel HTDS générique + mapping de la réponse ──────────────────
    @SuppressWarnings("unchecked")
    private OdmResponse call(String path, String inKey, Object body, String outKey) {
        Map<String, Object> htds = odm.post()
                .uri(path)
                .body(Map.of(inKey, body))
                .retrieve()
                .body(Map.class);

        Map<String, Object> decision = htds == null ? Map.of()
                : (Map<String, Object>) htds.getOrDefault(outKey, Map.of());

        Map<String, Object> details = new HashMap<>(decision);
        details.remove("decision");
        details.remove("rulesFired");

        return OdmResponse.builder()
                .decision((String) decision.get("decision"))
                .rulesTriggered((List<String>) decision.getOrDefault("rulesFired", List.of()))
                .details(details)
                .build();
    }

    // ── Helpers (alignés sur ton mock : minuscules + null) ────────────
    private double toDouble(Object v) {
        if (v == null) return 0.0;
        try { return Double.parseDouble(v.toString()); } catch (Exception e) { return 0.0; }
    }

    private int toInt(Object v) {
        if (v == null) return 0;
        try { return (int) Double.parseDouble(v.toString()); } catch (Exception e) { return 0; }
    }

    private String toStr(Object v) {
        if (v == null) return null;
        String s = v.toString().trim();
        if (s.isEmpty() || s.equalsIgnoreCase("null")
                || s.equalsIgnoreCase("none") || s.equalsIgnoreCase("nan")) return null;
        return s.toLowerCase();   // <-- résout le piège de casse ODM
    }
}