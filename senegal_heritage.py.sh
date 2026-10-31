# 1. Remplacer senegal_heritage.py par la version ci-dessus
# 2. Lancer le bootstrap (crée Dockerfile, fly.toml, .dockerignore)
python senegal_heritage.py bootstrap --app-name mon-studio-senegal-<ton-suffixe>

# 3. Commit + push (optionnel, pour garder l'historique)
git add Dockerfile fly.toml .dockerignore senegal_heritage.py
git commit -m "Intégration bootstrap Fly.io + corpus dynamique"
git push

# 4. Créer le volume persistant
fly volumes create studio_data --size 3 --region cdg

# 5. Déployer
fly deploy

# 6. Vérifier
curl https://mon-studio-senegal-<ton-suffixe>.fly.dev/health