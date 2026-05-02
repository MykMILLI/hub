from flask import render_template, request, redirect, url_for, current_app, flash, jsonify
from ..models import db, Recipe, ShoppingItem, ShoppingList, GroceryHistory
from ..blueprints import main_bp
from ..security import sanitize_text, sanitize_html, is_http_url, is_url_safe_for_fetch
from bs4 import BeautifulSoup
import json
import requests
from urllib.parse import urlparse, parse_qs, unquote
import re


def _as_list(value):
    if value is None:
        return []
    if isinstance(value, list):
        return value
    return [value]


def _string_from_image(value) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        for item in value:
            out = _string_from_image(item)
            if out:
                return out
    if isinstance(value, dict):
        return str(value.get('url') or value.get('contentUrl') or '').strip()
    return ''


def _extract_instruction_lines(raw) -> list[str]:
    lines = []
    for item in _as_list(raw):
        if isinstance(item, str):
            text = sanitize_text(item)
            if text:
                lines.append(text)
            continue
        if not isinstance(item, dict):
            continue
        if str(item.get('@type') or '').lower() == 'howtosection':
            lines.extend(_extract_instruction_lines(item.get('itemListElement')))
            continue
        text = sanitize_text(item.get('text') or item.get('name') or '')
        if text:
            lines.append(text)
            continue
        nested = item.get('itemListElement')
        if nested is not None:
            lines.extend(_extract_instruction_lines(nested))
    deduped = []
    seen = set()
    for line in lines:
        key = line.lower()
        if key in seen:
            continue
        seen.add(key)
        deduped.append(line)
    return deduped


def _extract_ingredient_lines(raw) -> list[str]:
    lines = []
    for item in _as_list(raw):
        if isinstance(item, str):
            text = sanitize_text(item)
            if text:
                lines.append(text)
            continue
        if isinstance(item, dict):
            text = sanitize_text(item.get('text') or item.get('name') or '')
            if text:
                lines.append(text)
    deduped = []
    seen = set()
    for line in lines:
        key = line.lower()
        if key in seen:
            continue
        seen.add(key)
        deduped.append(line)
    return deduped


def _extract_json_ld_recipe_nodes(soup: BeautifulSoup) -> list[dict]:
    nodes = []
    for script in soup.find_all('script', attrs={'type': 'application/ld+json'}):
        raw = (script.string or script.get_text() or '').strip()
        if not raw:
            continue
        try:
            payload = json.loads(raw)
        except Exception:
            continue

        candidates = []
        if isinstance(payload, list):
            candidates.extend(payload)
        elif isinstance(payload, dict):
            candidates.append(payload)
            graph = payload.get('@graph')
            if isinstance(graph, list):
                candidates.extend(graph)

        for node in candidates:
            if not isinstance(node, dict):
                continue
            node_type = node.get('@type')
            type_values = [str(t).lower() for t in _as_list(node_type)]
            if any(t == 'recipe' for t in type_values):
                nodes.append(node)
    return nodes


def _extract_recipe_from_html(html: str, url: str) -> dict:
    soup = BeautifulSoup(html, 'html.parser')
    recipe_nodes = _extract_json_ld_recipe_nodes(soup)

    title = ''
    photo_url = ''
    ingredients = []
    instruction_lines = []

    if recipe_nodes:
        best_ingredient_lines = []
        best_instruction_lines = []
        best_photo = ''
        best_title = ''

        for node in recipe_nodes:
            node_title = sanitize_text(node.get('name') or '')
            node_photo = sanitize_text(_string_from_image(node.get('image') or ''))
            node_ingredients = _extract_ingredient_lines(node.get('recipeIngredient') or node.get('ingredients') or [])
            node_instructions = _extract_instruction_lines(node.get('recipeInstructions'))

            if node_title and len(node_title) > len(best_title):
                best_title = node_title
            if node_photo and not best_photo:
                best_photo = node_photo
            if len(node_ingredients) > len(best_ingredient_lines):
                best_ingredient_lines = node_ingredients
            if len(node_instructions) > len(best_instruction_lines):
                best_instruction_lines = node_instructions

        title = best_title
        photo_url = best_photo
        ingredients = best_ingredient_lines
        instruction_lines = best_instruction_lines

    if not ingredients:
        ingredient_meta = soup.select('[itemprop="recipeIngredient"]')
        ingredients = [sanitize_text(node.get('content') or node.get_text(' ', strip=True)) for node in ingredient_meta]
        ingredients = [x for x in ingredients if x]

    if not ingredients:
        fallback_nodes = soup.select('.recipe-ingredients-item, .ingredients-item, [data-ingredient-name], [class*="ingredient"]')
        fallback = [sanitize_text(node.get_text(' ', strip=True)) for node in fallback_nodes]
        ingredients = [x for x in fallback if x and len(x) > 1][:80]

    if not ingredients:
        broad_nodes = soup.select(
            '[itemprop="recipeIngredient"], .ingredients li, .ingredient-list li, [id*="ingredient"] li, [class*="ingredient"] li'
        )
        broad = [sanitize_text(node.get_text(' ', strip=True)) for node in broad_nodes]
        ingredients = [x for x in broad if x and 1 < len(x) <= 180][:120]

    if not title:
        og_title = soup.find('meta', attrs={'property': 'og:title'})
        title = sanitize_text((og_title.get('content') if og_title else '') or '')
    if not title and soup.title:
        title = sanitize_text(soup.title.get_text(' ', strip=True) or '')

    if not photo_url:
        og_image = soup.find('meta', attrs={'property': 'og:image'})
        photo_url = sanitize_text((og_image.get('content') if og_image else '') or '')

    ingredients_html = ''
    if ingredients:
        ingredients_html = '<ul>' + ''.join(f'<li>{sanitize_text(item)}</li>' for item in ingredients) + '</ul>'

    instructions_html = ''
    if instruction_lines:
        instructions_html = '<ol>' + ''.join(f'<li>{sanitize_text(step)}</li>' for step in instruction_lines) + '</ol>'

    return {
        'title': title,
        'link': sanitize_text(url),
        'photo_url': photo_url,
        'ingredients_html': ingredients_html,
        'instructions_html': instructions_html,
    }


def _is_social_recipe_host(url: str) -> bool:
    host = (urlparse(url).hostname or '').lower()
    return any(token in host for token in ('pinterest.', 'pin.it', 'facebook.', 'fb.watch', 'm.facebook.'))


def _is_non_social_http_url(url: str) -> bool:
    if not is_http_url(url):
        return False
    host = (urlparse(url).hostname or '').lower()
    if not host:
        return False
    social_tokens = (
        'pinterest.', 'pin.it', 'facebook.', 'fb.watch', 'm.facebook.',
        'instagram.', 't.co', 'x.com', 'twitter.com'
    )
    return not any(token in host for token in social_tokens)


def _extract_social_source_candidates(url: str, html: str) -> list[str]:
    candidates: list[str] = []
    seen = set()

    def add_candidate(value: str):
        value = sanitize_text(unquote(str(value or '')).strip())
        if not value:
            return
        if value.startswith('//'):
            value = f'https:{value}'
        if not _is_non_social_http_url(value):
            return
        if not is_url_safe_for_fetch(value):
            return
        key = value.lower()
        if key in seen:
            return
        seen.add(key)
        candidates.append(value)

    parsed = urlparse(url)
    query = parse_qs(parsed.query or '')
    for key in ('url', 'u', 'link', 'target', 'redirect', 'redirect_uri'):
        for item in query.get(key, []):
            add_candidate(item)

    if html:
        soup = BeautifulSoup(html, 'html.parser')
        for sel in (
            'meta[property="al:web:url"]',
            'meta[property="og:url"]',
            'meta[property="article:publisher"]',
            'meta[name="twitter:url"]',
            'link[rel="canonical"]',
        ):
            for node in soup.select(sel):
                add_candidate(node.get('content') or node.get('href') or '')

        json_blob = ' '.join((script.string or script.get_text() or '') for script in soup.find_all('script'))
        if json_blob:
            for hit in re.findall(r'https?://[^\s"\'<>]+', json_blob):
                add_candidate(hit)

    return candidates


def _try_parse_social_source(url: str, html: str) -> dict | None:
    for candidate in _extract_social_source_candidates(url, html):
        try:
            response = requests.get(
                candidate,
                timeout=15,
                headers={
                    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36',
                    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
                    'Accept-Language': 'en-US,en;q=0.9',
                    'Cache-Control': 'no-cache',
                },
            )
            response.raise_for_status()
            parsed = _extract_recipe_from_html(response.text or '', candidate)
            has_recipe_content = bool(parsed.get('ingredients_html') or parsed.get('instructions_html'))
            if parsed.get('title') and has_recipe_content:
                return parsed
        except Exception:
            continue
    return None


def _fallback_social_recipe(url: str) -> dict:
    parsed = urlparse(url)
    host = (parsed.hostname or '').lower()
    query = parse_qs(parsed.query or '')

    title = ''
    if 'facebook' in host or 'fb.watch' in host:
        title = 'Recipe from Facebook'
        if query.get('story_fbid'):
            title = 'Facebook Recipe Post'
    elif 'pinterest' in host or 'pin.it' in host:
        title = 'Recipe from Pinterest'

    # Some social shares carry nested URL in query args (u, url, link)
    nested_url = ''
    for key in ('url', 'u', 'link'):
        if query.get(key):
            nested_url = sanitize_text(unquote(str(query.get(key)[0] or '')).strip())
            if is_http_url(nested_url):
                break
            nested_url = ''

    return {
        'title': title or 'Imported recipe link',
        'link': nested_url or sanitize_text(url),
        'photo_url': '',
        'ingredients_html': '',
        'instructions_html': '',
    }


@main_bp.route('/recipes', methods=['GET', 'POST'])
def recipes():
    if request.method == 'POST':
        recipe_id = request.form.get('recipe_id')
        title = sanitize_text(request.form['title'])
        link = sanitize_text(request.form.get('link'))
        photo_url = sanitize_text(request.form.get('photo_url'))
        if link and not is_http_url(link):
            flash('Invalid link URL.', 'error')
            recipes_list = Recipe.query.order_by(Recipe.timestamp.desc()).all()
            config = current_app.config['HOMEHUB_CONFIG']
            return render_template('recipes.html', recipes=recipes_list, config=config, form_title=title, form_link=link, form_photo_url=photo_url)
        if photo_url and not is_http_url(photo_url):
            photo_url = ''
        ingredients = sanitize_html(request.form.get('ingredients', ''))
        instructions = sanitize_html(request.form.get('instructions', ''))
        creator = sanitize_text(request.form['creator'])
        
        # Handle tags (JSON array)
        raw_tags = request.form.get('tags', '').strip()
        tags_list = []
        if raw_tags:
            try:
                tags_list = json.loads(raw_tags)
                if not isinstance(tags_list, list):
                    tags_list = []
            except Exception:
                tags_list = [t.strip() for t in raw_tags.split(',') if t.strip()]
        tags_list = [sanitize_text(t) for t in tags_list if isinstance(t, str) and t.strip()]
        
        if not (ingredients and ingredients.strip()) and not (instructions and instructions.strip()):
            flash('Please add ingredients or instructions (or both).', 'error')
            recipes_list = Recipe.query.order_by(Recipe.timestamp.desc()).all()
            config = current_app.config['HOMEHUB_CONFIG']
            return render_template('recipes.html', recipes=recipes_list, config=config, form_title=title, form_link=link, form_ingredients=ingredients or '', form_instructions=instructions or '')
        
        if recipe_id:
            rec = Recipe.query.get_or_404(int(recipe_id))
            rec.title = title
            rec.link = link
            rec.photo_url = photo_url
            rec.ingredients = ingredients
            rec.instructions = instructions
            rec.tags = json.dumps(tags_list)
            db.session.commit()
            flash('Recipe updated.', 'success')
            return redirect(url_for('main.recipes'))
        else:
            recipe = Recipe(title=title, link=link, photo_url=photo_url, ingredients=ingredients, instructions=instructions, creator=creator, tags=json.dumps(tags_list))
            db.session.add(recipe)
            db.session.commit()
            flash('Recipe added.', 'success')
            return redirect(url_for('main.recipes'))
    
    # Filter by tags if provided
    filter_tags = request.args.get('tags')
    recipes_list = Recipe.query.order_by(Recipe.timestamp.desc()).all()
    if filter_tags:
        try:
            selected = json.loads(filter_tags)
            if isinstance(selected, list) and selected:
                def match(item_tags):
                    try:
                        arr = json.loads(item_tags or '[]')
                    except Exception:
                        arr = []
                    return any(t in arr for t in selected)
                recipes_list = [r for r in recipes_list if match(r.tags)]
        except Exception:
            pass
    
    # Convert Recipe objects to dictionaries for JSON serialization in template
    recipes_dicts = []
    for r in recipes_list:
        recipes_dicts.append({
            'id': r.id,
            'title': r.title,
            'link': r.link,
            'photo_url': r.photo_url,
            'ingredients': r.ingredients,
            'instructions': r.instructions,
            'creator': r.creator,
            'timestamp': r.timestamp.isoformat() if r.timestamp else None,
            'tags': r.tags or '[]'
        })
    
    config = current_app.config['HOMEHUB_CONFIG']
    return render_template('recipes.html', recipes=recipes_list, recipes_json=recipes_dicts, config=config)


@main_bp.route('/recipes/edit/<int:recipe_id>')
def edit_recipe(recipe_id):
    rec = Recipe.query.get_or_404(recipe_id)
    recipes_list = Recipe.query.order_by(Recipe.timestamp.desc()).all()
    
    # Convert Recipe objects to dictionaries for JSON serialization
    recipes_dicts = []
    for r in recipes_list:
        recipes_dicts.append({
            'id': r.id,
            'title': r.title,
            'link': r.link,
            'photo_url': r.photo_url,
            'ingredients': r.ingredients,
            'instructions': r.instructions,
            'creator': r.creator,
            'timestamp': r.timestamp.isoformat() if r.timestamp else None,
            'tags': r.tags or '[]'
        })
    
    config = current_app.config['HOMEHUB_CONFIG']
    return render_template(
        'recipes.html',
        recipes=recipes_list,
        recipes_json=recipes_dicts,
        config=config,
        form_title=rec.title,
        form_link=rec.link,
        form_photo_url=rec.photo_url,
        form_ingredients=rec.ingredients,
        form_instructions=rec.instructions,
        form_recipe_id=rec.id,
        form_tags=rec.tags or '[]',
    )


@main_bp.route('/recipes/delete/<int:recipe_id>', methods=['POST'])
def delete_recipe(recipe_id):
    recipe = Recipe.query.get_or_404(recipe_id)
    user = sanitize_text(request.form['user'])
    admin_name = current_app.config['HOMEHUB_CONFIG'].get('admin_name', 'Administrator')
    admin_aliases = {admin_name, 'Administrator', 'admin'}
    if user in admin_aliases or user == recipe.creator:
        db.session.delete(recipe)
        db.session.commit()
        flash('Recipe deleted.', 'success')
    return redirect(url_for('main.recipes'))


@main_bp.route('/api/recipes/<int:recipe_id>/tags', methods=['POST'])
def update_recipe_tags(recipe_id):
    """Update tags for a recipe via API (similar to chores/shopping)"""
    recipe = Recipe.query.get_or_404(recipe_id)
    try:
        data = request.get_json(force=True) or {}
        tags = data.get('tags', [])
        if not isinstance(tags, list):
            tags = []
        cleaned = []
        for t in tags:
            if isinstance(t, str):
                cleaned.append(sanitize_text(t))
        recipe.tags = json.dumps(cleaned)
        db.session.commit()
        return jsonify({"ok": True, "id": recipe.id, "tags": cleaned})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 400


@main_bp.route('/api/recipes', methods=['GET'])
def api_get_recipes():
    """Get recipes with optional tag filtering"""
    tags = request.args.get('tags')
    items = Recipe.query.order_by(Recipe.timestamp.desc()).all()
    if tags:
        try:
            sel = json.loads(tags)
            if isinstance(sel, list) and sel:
                def match(item_tags):
                    try:
                        arr = json.loads(item_tags or '[]')
                    except Exception:
                        arr = []
                    return any(t in arr for t in sel)
                items = [i for i in items if match(i.tags)]
        except Exception:
            pass
    
    result = []
    for r in items:
        try:
            tags_arr = json.loads(r.tags or '[]')
        except Exception:
            tags_arr = []
        result.append({
            'id': r.id,
            'title': r.title,
            'link': r.link,
            'photo_url': r.photo_url,
            'creator': r.creator,
            'timestamp': r.timestamp.isoformat() if r.timestamp else None,
            'tags': tags_arr,
            'ingredients': r.ingredients,
            'instructions': r.instructions,
        })
    return jsonify(result)


@main_bp.route('/api/recipes/<int:recipe_id>', methods=['DELETE'])
def api_delete_recipe(recipe_id: int):
    recipe = Recipe.query.get_or_404(recipe_id)
    data = request.get_json(silent=True) or {}
    user = sanitize_text(data.get('user') or request.args.get('user') or '').strip()
    admin_name = current_app.config['HOMEHUB_CONFIG'].get('admin_name', 'Administrator')
    admin_aliases = {admin_name, 'Administrator', 'admin'}

    if user not in admin_aliases and user != recipe.creator:
        return jsonify({'ok': False, 'error': 'Permission denied'}), 403

    db.session.delete(recipe)
    db.session.commit()
    return jsonify({'ok': True})


@main_bp.route('/api/recipes/parse-link', methods=['POST'])
def api_parse_recipe_link():
    data = request.get_json(silent=True) or {}
    url = sanitize_text(data.get('url') or '').strip()

    if not url:
        return jsonify({'ok': False, 'error': 'Recipe URL is required'}), 400
    if not is_http_url(url):
        return jsonify({'ok': False, 'error': 'Invalid URL'}), 400
    if not is_url_safe_for_fetch(url):
        return jsonify({'ok': False, 'error': 'URL is not allowed'}), 400

    html_text = ''
    try:
        response = requests.get(
            url,
            timeout=15,
            headers={
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36',
                'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
                'Accept-Language': 'en-US,en;q=0.9',
                'Cache-Control': 'no-cache',
            },
        )
        response.raise_for_status()
        html_text = response.text or ''
    except Exception as exc:
        if _is_social_recipe_host(url):
            return jsonify({'ok': True, 'recipe': _fallback_social_recipe(url)})
        return jsonify({'ok': False, 'error': f'Unable to fetch recipe URL: {exc}'}), 400

    parsed = _extract_recipe_from_html(html_text, url)
    if _is_social_recipe_host(url):
        has_recipe_content = bool(parsed.get('ingredients_html') or parsed.get('instructions_html'))
        if not has_recipe_content:
            source_parsed = _try_parse_social_source(url, html_text)
            if source_parsed:
                parsed = source_parsed

    if not parsed.get('title'):
        if _is_social_recipe_host(url):
            parsed = _fallback_social_recipe(url)
        else:
            return jsonify({'ok': False, 'error': 'Could not parse recipe details from this URL'}), 400

    # Social links often only provide generic metadata; keep whatever we can parse.
    if _is_social_recipe_host(url) and not parsed.get('title'):
        parsed = _fallback_social_recipe(url)

    if not parsed.get('title'):
        return jsonify({'ok': False, 'error': 'Could not parse recipe details from this URL'}), 400

    return jsonify({'ok': True, 'recipe': parsed})


@main_bp.route('/api/recipes/autosave', methods=['POST'])
def api_recipe_autosave():
    data = request.get_json(silent=True) or {}
    creator = sanitize_text(data.get('creator') or '').strip()
    recipe_id_raw = data.get('recipe_id')

    if not creator:
        return jsonify({'ok': False, 'error': 'Creator is required'}), 400

    title = sanitize_text(data.get('title') or '').strip() or 'Untitled Recipe'
    link = sanitize_text(data.get('link') or '').strip()
    photo_url = sanitize_text(data.get('photo_url') or '').strip()
    ingredients = sanitize_html(data.get('ingredients') or '')
    instructions = sanitize_html(data.get('instructions') or '')
    tags = data.get('tags', [])
    if not isinstance(tags, list):
        tags = []
    tags = [sanitize_text(t) for t in tags if isinstance(t, str) and sanitize_text(t)]

    if link and not is_http_url(link):
        return jsonify({'ok': False, 'error': 'Invalid link URL'}), 400
    if photo_url and not is_http_url(photo_url):
        photo_url = ''

    recipe = None
    if recipe_id_raw not in (None, ''):
        try:
            recipe = Recipe.query.get(int(recipe_id_raw))
        except Exception:
            recipe = None

    if recipe is None:
        recipe = Recipe(
            title=title,
            creator=creator,
        )
        db.session.add(recipe)

    recipe.title = title
    recipe.link = link
    recipe.photo_url = photo_url
    recipe.ingredients = ingredients
    recipe.instructions = instructions
    recipe.tags = json.dumps(tags)
    db.session.commit()

    return jsonify({
        'ok': True,
        'id': recipe.id,
        'updated_at': recipe.timestamp.isoformat() if recipe.timestamp else None,
    })


@main_bp.route('/api/recipes/<int:recipe_id>/add-ingredients-to-shopping', methods=['POST'])
def api_add_recipe_ingredients_to_shopping(recipe_id: int):
    recipe = Recipe.query.get_or_404(recipe_id)
    creator = sanitize_text(request.args.get('creator') or request.form.get('creator') or '').strip()
    data = request.get_json(silent=True) or {}
    if not creator:
        creator = sanitize_text(data.get('creator') or '').strip()

    list_id = data.get('list_id')
    target_list = None
    if list_id not in (None, ''):
        try:
            target_list = ShoppingList.query.get(int(list_id))
        except Exception:
            target_list = None
    if not target_list:
        target_list = ShoppingList.query.order_by(ShoppingList.created_at.asc(), ShoppingList.id.asc()).first()
    if not target_list:
        target_list = ShoppingList(name='General', color='#3B82F6', creator='System')
        db.session.add(target_list)
        db.session.flush()

    from_html = BeautifulSoup(recipe.ingredients or '', 'html.parser')
    ingredient_lines = [sanitize_text(li.get_text(' ', strip=True)) for li in from_html.select('li')]
    if not ingredient_lines:
        ingredient_lines = [sanitize_text(x) for x in from_html.get_text('\n').split('\n')]
    ingredient_lines = [line for line in ingredient_lines if line]

    if not ingredient_lines:
        return jsonify({'ok': False, 'error': 'Recipe has no ingredients to add'}), 400

    created = 0
    for line in ingredient_lines:
        row = ShoppingItem(item=line, creator=creator or recipe.creator or 'Family', list_id=target_list.id, checked=False)
        db.session.add(row)
        db.session.add(GroceryHistory(item=line, creator=creator or recipe.creator or 'Family'))
        created += 1

    db.session.commit()
    return jsonify({'ok': True, 'created': created, 'list_id': target_list.id, 'list_name': target_list.name})
