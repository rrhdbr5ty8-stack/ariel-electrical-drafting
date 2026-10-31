# ג'וב – Job OS

הרצה (פקודה אחת):
```
python3 /workspace/drafting_toolkit/tools/run_pipeline.py <תיקיית הג'וב>
```

## שלושה סוגי קלט (שווים בחשיבות) – שים קובץ אחד ב-`inputs/`
| סוג | קובץ | מה קורה |
|---|---|---|
| DWG/DXF | `inputs/plan.dwg` | ייבוא רקע אדריכלי אפור (A-ARCH), יחידות אוטומטיות |
| PDF | `inputs/plan.pdf` | עמוד → תמונה → underlay מכויל |
| סקיצה | `inputs/sketch.jpg/png/webp` | יישור פרספקטיבה + ניקוי → underlay מכויל |

ל-PDF/סקיצה: מלא `input.calibration` ב-`job.yaml` (width_m או שתי נקודות + dist_m). בלי כיול → 10 מ' + אזהרה.
אם יש כמה קבצים: `--input inputs/x.pdf` או `input.file` ב-job.yaml.

## קבצים
- `circuits.csv` – טבלת מעגלים → סכמה חד-קווית
- `placements.json` – רשימת סמלים: `[{"key":"SOCKET_2","x":450,"y":12.5,"rot":0,"circuit":4,"height":30}]` (ס"מ; רשימת מפתחות: `tools/symbol_editor.py list`)
- `out/` – PDF/DXF/DWG + `deliverables.zip` (רק אם שער העברית עבר) + `pipeline_report.json`

## כתב כמויות וסימון (D) – סעיף `boq:` ב-job.yaml
```yaml
boq:
  enabled_chapters: ["08", "15", "18", "34"]   # --chapters גובר
  # inputs: [inputs/plan.pdf]                  # ברירת מחדל: out/E-01_*.dxf, אחרת inputs/*
  mifrat:   {DATA: "18-5.3", SMOKE: "34#21"}   # מפתח סמל -> קוד מפרט כללי (הפרק נגזר מהקוד)
  chapters: {TV: "18", PANEL_COMM: "18"}       # מפתח סמל -> פרק בלבד
```
הרצה: `python3 tools/run_boq.py /workspace/jobs/<job>` → `out/boq_d/`. מדריך: `docs/BOQ_MARKUP.md`.

## קליטת IFC (מקומי, בלי Autodesk APS)

אין רישיון Revit ואין APS. IfcOpenShell קורא IFC2x3 או IFC4, ו-ezdxf כותב רקע אדריכלי אפור שהפייפליין מייבא כמו כל DXF.

```
python3 -m pip install -r tools/requirements-ifc.txt
python3 tools/ifc_ingest.py <קובץ.ifc> <תיקיית הג'וב> --create-job
python3 tools/run_pipeline.py <תיקיית הג'וב>
```

דוגמה:

```
python3 tools/ifc_ingest.py /workspace/jobs/incoming/herzl.ifc /workspace/jobs/herzl_10 --create-job
python3 tools/run_pipeline.py /workspace/jobs/herzl_10
```

מה נכתב בתיקיית הג'וב:

| קובץ | תוכן |
|---|---|
| `inputs/arch_from_ifc.dxf` | קירות `A-WALL`, דלתות `A-DOOR`, חלונות `A-WIND`. צבע אפור (ACI 8). יחידות ס"מ (`$INSUNITS` = 5). הפינה המינימלית של התוכנית מוזזת ל-(0,0) כדי להתיישר עם `cad_base: null`. |
| `job.yaml` | `job.project` / `client` / `address` מ-IfcProject / IfcSite / IfcBuilding (עברית נשמרת). `input.type: cad`, `input.file: inputs/arch_from_ifc.dxf`. `rooms`: `{x, y, name, area}` — x,y בס"מ, שטח במ"ר. |
| `circuits.csv` | רק אם הקובץ חסר: שורת כותרת `no,desc,breaker,poles,cable,phase,group,kw,length,note`. טבלה קיימת לא נדרסת. |
| `inputs/ifc_electrical.json` | אביזרי חשמל אם יש (IfcOutlet, IfcLightFixture, IfcSwitchingDevice, IfcElectricDistributionBoard, IfcCableSegment וקרובים). |
| `placements.json` | נוצר רק אם נמצאו סמלים ואין קובץ קיים. המפתחות ניחוש — לאמת מול `python3 tools/symbol_editor.py list` לפני הפייפליין. `--no-placements` מדלג. `--force-placements` דורס. |

`--keep-world-coords` משאיר את קואורדינטות ה-IFC בלי הזזה (למשל מגרש עם רשת קואורדינטות, כשמגדירים `cad_base` ידנית).

קובץ IFC אמיתי מגיע מהאדריכל. בדיקת העשן האוטומטית מייצרת מודל קטן (חדר, קיר, דלת, חלון, שקע) בלי לשמור בינארי בריפו:

```
python3 -m pytest tests/test_ifc_ingest.py -q
```
