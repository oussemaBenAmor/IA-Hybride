# === Destination : app/schemas/business.py (remplace l'existant) ===
"""
Schémas Pydantic des paramètres métier par cas.

Enrichis avec des SOUS-OPÉRATIONS (operation / type_virement) pour couvrir des
cas réels au-delà du seul « calcul d'éligibilité » : opposition vs plafond pour
une carte, rachat vs souscription pour l'assurance vie, etc. Les sous-opérations
sont des chaînes libres (avec valeurs attendues dans la description) pour rester
tolérantes à l'extraction LLM ; l'ODM gère les valeurs inconnues.

Note : l'ancienne classe `workflow` a été retirée — elle dupliquait GraphState
(app/graph/state.py), qui est la seule source de vérité de l'état du graphe.
"""
from typing import Optional
from pydantic import BaseModel, Field
from enum import Enum


class BusinessCase(str, Enum):
    CREDIT_IMMOBILIER   = "credit_immobilier"
    CREDIT_CONSOMMATION = "credit_consommation"
    ASSURANCE_VIE       = "assurance_vie"
    CARTE_BANCAIRE      = "carte_bancaire"
    VIREMENT            = "virement"
    HORS_PERIMETRE      = "hors_perimetre"


class CreditImmobilierParams(BaseModel):
    montant:        Optional[float] = Field(None, description="Montant du crédit en euros")
    duree_mois:     Optional[int]   = Field(None, description="Durée du crédit en mois")
    revenu_mensuel: Optional[float] = Field(None, description="Revenu mensuel net en euros")
    apport:         Optional[float] = Field(None, description="Apport personnel en euros")
    age:            Optional[int]   = Field(None, description="Âge de l'emprunteur")


class CreditConsommationParams(BaseModel):
    montant:        Optional[float] = Field(None, description="Montant du crédit en euros")
    duree_mois:     Optional[int]   = Field(None, description="Durée en mois")
    revenu_mensuel: Optional[float] = Field(None, description="Revenu mensuel net en euros")
    motif:          Optional[str]   = Field(None, description="Motif du crédit (voiture, travaux, loisirs...)")


class AssuranceVieParams(BaseModel):
    operation:         Optional[str]   = Field(None, description="Opération : souscription, rachat ou arbitrage")
    montant_initial:   Optional[float] = Field(None, description="Versement initial en euros")
    versement_mensuel: Optional[float] = Field(None, description="Versement mensuel en euros")
    duree_ans:         Optional[int]   = Field(None, description="Durée du contrat en années")
    age:               Optional[int]   = Field(None, description="Âge du souscripteur")


class CarteBancaireParams(BaseModel):
    operation:       Optional[str]   = Field(None, description="Opération : opposition, plafond, renouvellement ou deblocage")
    type_carte:      Optional[str]   = Field(None, description="Type de carte : visa ou mastercard")
    plafond_souhaite: Optional[float] = Field(None, description="Plafond souhaité en euros (si opération plafond)")


class VirementParams(BaseModel):
    type_virement: Optional[str]   = Field(None, description="Type : sepa, instantane, international ou programme")
    montant:       Optional[float] = Field(None, description="Montant du virement en euros")
    beneficiaire:  Optional[str]   = Field(None, description="Nom du bénéficiaire")
    iban:          Optional[str]   = Field(None, description="IBAN du bénéficiaire")


# Mapping cas métier → modèle Pydantic
CASE_SCHEMA_MAP: dict[str, type[BaseModel]] = {
    "credit_immobilier":   CreditImmobilierParams,
    "credit_consommation": CreditConsommationParams,
    "assurance_vie":       AssuranceVieParams,
    "carte_bancaire":      CarteBancaireParams,
    "virement":            VirementParams,
}