version "6.3"
// ScoutOSS project-level DicomEdit script (second layer of de-identification).
//
// Primary de-identification happens BEFORE XNAT in the ScoutOSS DICOM gateway
// (HMAC-SHA256 pseudonyms, per-project date shift, PS3.15 Basic Profile; DicomEdit has
// no keyed-hash function). This script re-applies the removals so that anything that
// reaches this project by another route (web upload, direct C-STORE) is still stripped.
// The gateway's pseudonymised PatientID/PatientName and shifted dates are preserved.
-(0010,1000)
-(0010,1001)
-(0010,1002)
-(0010,1040)
-(0010,1060)
-(0010,2154)
-(0010,2160)
-(0010,21B0)
-(0010,21F0)
-(0010,0032)
-(0010,1005)
-(0010,0021)
-(0008,0081)
-(0008,0092)
-(0008,0094)
-(0008,1040)
-(0008,1048)
-(0008,1050)
-(0008,1060)
-(0008,1070)
-(0008,0082)
-(0008,1010)
-(0018,1000)
-(0032,1032)
-(0032,1033)
-(0032,4000)
-(0038,0010)
-(0038,0300)
-(0038,0400)
-(0040,0006)
-(0040,0243)
-(0040,0253)
-(0040,1001)
-(0040,2016)
-(0040,2017)
-(0040,A123)
-(4008,0114)
(0008,0090) := ""
(0010,0030) := ""
(0008,0050) := ""
(0020,0010) := ""
removeAllPrivateTags
