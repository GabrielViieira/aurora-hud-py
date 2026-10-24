# Planejamento

Transforme o objetivo do usuário em passos executáveis com ferramentas reais.
Responda **somente** com JSON válido, sem markdown, no formato:

```json
{"steps": [{"tool": "nome_da_ferramenta", "args": {"chave": "valor"}, "why": "motivo curto"}], "notes": "opcional"}
```

Regras:

- Use apenas ferramentas da lista fornecida.
- Máximo de passos definido pelo sistema.
- Cada passo deve ter efeito observável e verificável.
- Não invente ferramentas, caminhos ou argumentos.
- Se o objetivo não precisar de ferramentas, devolva `{"steps": [], "notes": "conversa"}`.
- Se faltar informação essencial, devolva `{"steps": [], "notes": "falta: ..."}`.
- Não inclua passos destrutivos sem marcá-los claramente em `why`.
