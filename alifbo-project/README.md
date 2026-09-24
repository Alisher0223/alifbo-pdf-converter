# Alifbo — Uzbek PDF Converter

Flask ilovasi: o'zbek tilidagi PDF hujjatlardagi matnni konvertatsiya qiladi
(masalan, o'/g' kabi belgilarni to'g'ri shriftlar bilan qayta chizadi).

## O'rnatish

```bash
python -m venv venv
source venv/bin/activate   # Windows: venv\Scripts\activate
pip install -r requirements.txt
```

## Ishga tushirish

```bash
python app.py
```

Server manzili: http://127.0.0.1:5000

## Muhit o'zgaruvchilari

- `PORT` — server porti (standart: 5000)
- `FLASK_DEBUG` — `1` bo'lsa debug rejimi yoqiladi
