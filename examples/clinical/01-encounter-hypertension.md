---
status: active
premises:
  - id: renal-clearance-normal
    statement: Patient has normal renal clearance (estimated GFR > 60 mL/min)
    status: active
alternatives:
  - name: Amlodipine
    disposition: rejected
    reason: Patient has documented history of severe peripheral bilateral edema with calcium channel blockers
    premises:
      - renal-clearance-normal
---

# ENC-4812: Initial Hypertension Management

## 1. Clinical Context
Patient #4812 (62yo) presents with Stage 2 Essential Hypertension (158/96 mmHg). Baseline metabolic panel shows serum creatinine 0.9 mg/dL and eGFR 85 mL/min/1.73m².

## 2. Decision Outcome
Initiate Enalapril 10mg PO once daily. Target BP < 130/80 mmHg with repeat renal panel in 6 weeks.

## 3. Evaluated Alternatives
- **Amlodipine (Calcium Channel Blocker)**: Rejected due to prior clinical documentation of severe peripheral edema. Viable if renal failure develops (eGFR < 30) or severe hyperkalemia contraindicates ACE inhibitors.
