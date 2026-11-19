"""DICOM PS3.15 Annex E attribute actions used by ScoutOSS.

Profile = Basic Application Level Confidentiality Profile (113100) plus options:

* Retain Longitudinal Temporal Information, Modified Dates (113107): every DA/DT is
  shifted by the project offset instead of removed.
* Retain Patient Characteristics (113108): sex, age, size, weight, ethnic group,
  smoking/pregnancy status are kept.
* Clean Descriptors (113105): study/series/protocol descriptions are kept but scrubbed
  by the shared PHI redactor.

Anything not listed falls through to generic rules applied by VR in ``deidentify.py``:
UI -> hashed, DA/DT -> shifted, PN -> emptied; private tags and overlay/curve groups are
removed.
"""

from __future__ import annotations

from enum import StrEnum


class Action(StrEnum):
    REMOVE = "X"
    ZERO = "Z"
    DUMMY = "D"
    UID = "U"
    CLEAN = "C"
    KEEP = "K"


_X = Action.REMOVE
_Z = Action.ZERO
_C = Action.CLEAN
_K = Action.KEEP

ACTIONS: dict[str, Action] = {
    # --- identity (PatientID / PatientName are replaced by the pseudonym explicitly)
    "PatientID": Action.DUMMY,
    "PatientName": Action.DUMMY,
    "PatientBirthDate": _Z,
    "PatientBirthTime": _X,
    "PatientBirthName": _X,
    "PatientMotherBirthName": _X,
    "OtherPatientIDs": _X,
    "OtherPatientIDsSequence": _X,
    "OtherPatientNames": _X,
    "IssuerOfPatientID": _X,
    "PatientAddress": _X,
    "PatientTelephoneNumbers": _X,
    "PatientReligiousPreference": _X,
    "PatientInsurancePlanCodeSequence": _X,
    "PatientPrimaryLanguageCodeSequence": _X,
    "PatientState": _X,
    "PatientTransportArrangements": _X,
    "PatientComments": _X,
    "MilitaryRank": _X,
    "BranchOfService": _X,
    "Occupation": _X,
    "CountryOfResidence": _X,
    "RegionOfResidence": _X,
    "MedicalRecordLocator": _X,
    "MedicalAlerts": _X,
    "Allergies": _X,
    "AdditionalPatientHistory": _X,
    "SpecialNeeds": _X,
    "ReferencedPatientSequence": _X,
    "ReferencedPatientAliasSequence": _X,
    "ConfidentialityConstraintOnPatientDataDescription": _X,
    "InsurancePlanIdentification": _X,
    "ResponsiblePerson": _X,
    "ResponsibleOrganization": _X,
    # --- retained patient characteristics (option 113108)
    "PatientSex": _K,
    "PatientSexNeutered": _K,
    "PatientAge": _K,
    "PatientSize": _K,
    "PatientWeight": _K,
    "EthnicGroup": _K,
    "SmokingStatus": _K,
    "PregnancyStatus": _K,
    # --- visit / order
    "AccessionNumber": _Z,
    "StudyID": _Z,
    "StudyIDIssuer": _X,
    "AdmissionID": _X,
    "IssuerOfAdmissionID": _X,
    "AdmittingDiagnosesDescription": _X,
    "AdmittingDiagnosesCodeSequence": _X,
    "DischargeDiagnosisDescription": _X,
    "ServiceEpisodeID": _X,
    "ServiceEpisodeDescription": _X,
    "CurrentPatientLocation": _X,
    "VisitComments": _X,
    "ReasonForStudy": _X,
    "RequestedProcedureID": _X,
    "RequestedProcedureComments": _X,
    "RequestAttributesSequence": _X,
    "RequestingPhysician": _X,
    "RequestingService": _X,
    "PlacerOrderNumberImagingServiceRequest": _X,
    "FillerOrderNumberImagingServiceRequest": _X,
    "ImagingServiceRequestComments": _X,
    "OrderCallbackPhoneNumber": _X,
    "OrderEnteredBy": _X,
    "OrderEntererLocation": _X,
    "PerformedProcedureStepID": _X,
    "PerformedLocation": _X,
    "CommentsOnThePerformedProcedureStep": _X,
    # --- staff
    "ReferringPhysicianName": _Z,
    "ReferringPhysicianAddress": _X,
    "ReferringPhysicianTelephoneNumbers": _X,
    "ReferringPhysicianIdentificationSequence": _X,
    "PerformingPhysicianName": _X,
    "PerformingPhysicianIdentificationSequence": _X,
    "NameOfPhysiciansReadingStudy": _X,
    "PhysiciansReadingStudyIdentificationSequence": _X,
    "PhysiciansOfRecord": _X,
    "PhysiciansOfRecordIdentificationSequence": _X,
    "OperatorsName": _X,
    "OperatorIdentificationSequence": _X,
    "ScheduledPerformingPhysicianName": _X,
    "PhysicianApprovingInterpretation": _X,
    "ContentCreatorName": _Z,
    "ContentCreatorIdentificationCodeSequence": _X,
    "VerifyingObserverName": _Z,
    "VerifyingObserverSequence": _X,
    "VerifyingObserverIdentificationCodeSequence": _X,
    "VerifyingOrganization": _X,
    "PersonName": _X,
    "PersonAddress": _X,
    "PersonTelephoneNumbers": _X,
    "ParticipantSequence": _X,
    "AuthorObserverSequence": _X,
    "ActualHumanPerformersSequence": _X,
    "NamesOfIntendedRecipientsOfResults": _X,
    "IntendedRecipientsOfResultsIdentificationSequence": _X,
    "DistributionName": _X,
    "DistributionAddress": _X,
    # --- institution / device (InstitutionName becomes a coarse site code)
    "InstitutionName": Action.DUMMY,
    "InstitutionAddress": _X,
    "InstitutionCodeSequence": _X,
    "InstitutionalDepartmentName": _X,
    "CustodialOrganizationSequence": _X,
    "StationName": _X,
    "DeviceSerialNumber": _X,
    "GantryID": _X,
    "GeneratorID": _X,
    # --- free text: removed
    "StudyComments": _X,
    "TextComments": _X,
    "TextString": _X,
    "IdentifyingComments": _X,
    "FrameComments": _X,
    "ImagePresentationComments": _X,
    "AcquisitionComments": _X,
    "ResultsComments": _X,
    "Impressions": _X,
    "InterpretationText": _X,
    "InterpretationDiagnosisDescription": _X,
    "InterpretationAuthor": _X,
    "InterpretationRecorder": _X,
    "InterpretationTranscriber": _X,
    "InterpretationApproverSequence": _X,
    "InterpretationIDIssuer": _X,
    "ResultsIDIssuer": _X,
    "ResultsDistributionListSequence": _X,
    "ContributionDescription": _X,
    "TopicAuthor": _X,
    "TopicKeywords": _X,
    "TopicSubject": _X,
    "TopicTitle": _X,
    "Arbitrary": _X,
    "DigitalSignaturesSequence": _X,
    "DataSetTrailingPadding": _X,
    "OriginalAttributesSequence": _X,
    "ModifiedAttributesSequence": _X,
    "AcquisitionContextSequence": _X,
    # --- descriptors: cleaned (option 113105)
    "StudyDescription": _C,
    "SeriesDescription": _C,
    "ProtocolName": _C,
    "ImageComments": _C,
    "DerivationDescription": _C,
    "RequestedProcedureDescription": _C,
    "PerformedProcedureStepDescription": _C,
    "ScheduledProcedureStepDescription": _C,
    "AcquisitionDeviceProcessingDescription": _C,
}

# Modalities that carry free-text clinical content we do not de-identify.
REJECTED_MODALITIES = frozenset({"SR", "DOC", "KO", "PR"})

# DCM codes for DeidentificationMethodCodeSequence.
METHOD_CODES: tuple[tuple[str, str], ...] = (
    ("113100", "Basic Application Confidentiality Profile"),
    ("113107", "Retain Longitudinal Temporal Information Modified Dates Option"),
    ("113108", "Retain Patient Characteristics Option"),
    ("113105", "Clean Descriptors Option"),
)
