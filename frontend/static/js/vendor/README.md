# Bibliotecas servidas localmente

## tailwind-3.4.17.js

Cópia byte a byte de `https://cdn.tailwindcss.com/3.4.17` (Play CDN do Tailwind 3),
baixada em 16/09/2026. SHA-384 (base64):

    igm5BeiBt36UU4gqwWS7imYmelpTsZlQ45FZf+XBn9MuJbn4nQr7yx1yFydocC/K

Por que está aqui e não no CDN: as outras bibliotecas de terceiros levam
`integrity` + `crossorigin` (SRI), e o navegador só aceita SRI em recurso de
outra origem se o servidor mandar `Access-Control-Allow-Origin`. O
cdn.tailwindcss.com não manda, então SRI nele faria o navegador recusar o
script e a tela subir sem estilo nenhum. Servido da mesma origem, o arquivo
não precisa de SRI. Era a mesma versão que o CDN sem número servia naquele
dia, então nada mudou no visual.

Para trocar de versão: baixe `https://cdn.tailwindcss.com/<versão>`, confira o
hash, substitua o arquivo e atualize o `src` em `layout/base.html`,
`pages/login.html`, `pages/forgot_password.html` e `pages/reset_password.html`.
