# Gege le Coach — Roadmap

## Simple (prochaine session)

- [ ] **Commande `aide`** — l'utilisateur envoie "aide" et reçoit un guide d'utilisation du bot
- [ ] **Commande `pause` / `reprendre`** — coupe les reminders et check-ins (vacances, blessure)
- [ ] **Streak et gamification** — mentionner les séries de semaines sans séance ratée dans les rapports

## Moyen

- [ ] **Objectifs utilisateur** — demander pendant l'onboarding pourquoi il fait du sport (perte de poids, marathon, santé...) et personnaliser tous les messages en conséquence
- [ ] **Mémoire long terme** — référencer l'historique des semaines passées dans les messages libres et les encouragements
- [ ] **Semaines sans sport** — si l'utilisateur est en pause, ne pas envoyer de check-ins ni de rappels

## Complexe

- [ ] **Modifier son planning** — détecter "annule samedi" ou "pas de running cette semaine" et mettre à jour les sessions
- [ ] **Reporter une séance** — détecter "je fais running demain à la place" et déplacer la session

## Fait ✅

- [x] Onboarding : demander le blaze, présentation de Gege
- [x] Collecte du planning en plusieurs messages (sport ou heure manquant)
- [x] Boucle "t'as une autre séance ?" jusqu'au non
- [x] Reminder 30 min avant (one-way, sans question)
- [x] Check-in du soir avec commentaire et stats
- [x] Rapport hebdo et mensuel
- [x] Messages variés pour séances ratées (3 registres + stat obésité)
- [x] Persona Gemini (mec de 22 ans, Paris, argot 2026)
- [x] Messages libres restreints au sport uniquement
