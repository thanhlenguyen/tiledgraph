# Create new branch → work → commit → push branch → Pull Request → merge on GitHub → git pull origin main

Here’s the complete simple workflow with exact Git commands:
## 1. Start from latest main
```bash
git checkout main
git pull origin main
```
## 2. Create and switch to a new branch
```Bash
git checkout -b gps
```
(replace gps with your branch name, e.g. feature/login, fix-map, etc.)
## 3. Do your work
Edit files as usual.
## 4. Stage and commit
```Bash
git add .
git commit -m "Add GPS feature"
```
You can commit several times while working:
```Bash
git add .
git commit -m "Update GPS calculation"
```
## 5. Push the branch to GitHub
```Bash
git push -u origin gps
```
## 6. Create Pull Request on GitHub
- Open the link that appears in the terminal, or
- Go to: https://github.com/thanhlenguyen/tiledgraph/pull/new/gps
- Base: main
- Compare: gps
- Click Create pull request
## 7. Merge the Pull Request on GitHub
Click the green Merge pull request button → Confirm merge.
## 8. Update your local main
```Bash
git checkout main
git pull origin main
```
Optional cleanup after merging
```Bash
# Delete local branch
git branch -d gps

# Delete remote branch
git push origin --delete gps
```
## One-line summary (cheat sheet)
```Bash
git checkout main
git pull origin main
git checkout -b my-feature
# ... do work ...
git add .
git commit -m "Description of changes"
git push -u origin my-feature
# → Create & Merge Pull Request on GitHub
git checkout main
git pull origin main
```

## Short Cheat Sheet (Rebase version)
```Bash
git checkout main
git pull origin main
git checkout -b my-feature

# ... work + commit ...

git checkout main
git pull origin main
git checkout my-feature
git rebase main

git push -u origin my-feature
# → Create & Merge PR on GitHub

git checkout main
git pull origin main
```