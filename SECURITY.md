# Sicurezza

## Segnalare una vulnerabilità

Non pubblicare chiavi API, dati di lega privati o dettagli sfruttabili in una issue.
Usa la funzione **Report a vulnerability** nella scheda *Security* del repository.
Se la segnalazione non è sensibile, una normale issue con il template bug va bene.

Il progetto mantiene la versione più recente pubblicata. Non sono promessi tempi di
risposta da prodotto commerciale, ma le segnalazioni riproducibili vengono valutate
appena possibile.

## Dati e segreti

Fantabuddy salva cache e warehouse in locale. Prima di condividere log o artifact,
verifica che non contengano chiavi provider, percorsi personali o dati della tua lega.
La chiave API va passata tramite variabile d'ambiente, `.env` ignorato o file privato
come descritto nel README.
