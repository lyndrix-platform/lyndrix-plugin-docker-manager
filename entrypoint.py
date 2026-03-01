import json
import requests
import concurrent.futures
from nicegui import ui, run
from core.components.plugins.logic.models import ModuleManifest
from ui.layout import main_layout

# ==========================================
# 1. MANIFEST
# ==========================================
manifest = ModuleManifest(
    id="lyndrix.plugin.docker",
    name="Docker Manager",
    version="2.1.0",
    description="Überwacht Live-Docker-Container im Swarm/Cluster.",
    author="Lyndrix",
    icon="view_in_ar",
    type="PLUGIN",
    ui_route="/docker", 
    permissions={"subscribe": ["vault:ready_for_data"], "emit": []} # FIX: Rechte hinzugefügt
)

# ==========================================
# 2. PLUGIN STATE
# ==========================================
plugin_state = {
    "hosts": [] 
}

# ==========================================
# 3. LOGIK: DATEN ABRUFEN
# ==========================================
def fetch_single_host(host):
    host_conts = []
    url = f"http://{host['ip']}:{host['port']}/containers/json?all=1"
    try:
        response = requests.get(url, timeout=1.5)
        if response.status_code == 200:
            for c in response.json():
                name = c.get('Names', ['/unknown'])[0].lstrip('/')
                host_conts.append({
                    'id': c.get('Id')[:12],
                    'name': name,
                    'image': c.get('Image'),
                    'state': c.get('State'),
                    'status': c.get('Status'),
                })
        else:
            host_conts.append({'id': 'ERROR', 'name': f'HTTP {response.status_code}', 'image': '-', 'state': 'error', 'status': 'Unreachable'})
    except Exception as e:
        host_conts.append({'id': 'ERROR', 'name': 'NODE OFFLINE', 'image': '-', 'state': 'error', 'status': 'Connection Timeout'})
    
    return host['name'], host_conts

def fetch_all_containers_parallel():
    results = {}
    hosts = plugin_state.get("hosts", [])
    if not hosts:
        return {}
        
    with concurrent.futures.ThreadPoolExecutor(max_workers=15) as executor:
        futures = [executor.submit(fetch_single_host, h) for h in hosts]
        for future in concurrent.futures.as_completed(futures):
            host_name, conts = future.result()
            results[host_name] = conts
    return dict(sorted(results.items()))

# ==========================================
# 4. SETUP: ROUTEN & UI REGISTRIEREN
# ==========================================
def setup(ctx):
    ctx.log.info("STARTUP: Loading Docker Manager Interface...")

    # --- FIX: AUF DEN VAULT WARTEN ---
    @ctx.subscribe('vault:ready_for_data')
    async def load_hosts_from_vault(payload=None):
        ctx.log.info("LOAD: Vault ready. Fetching Docker hosts...")
        stored_hosts = ctx.get_secret("configured_hosts")
        if stored_hosts:
            try:
                plugin_state["hosts"] = json.loads(stored_hosts)
                ctx.log.info(f"SUCCESS: {len(plugin_state['hosts'])} hosts loaded from Vault.")
            except Exception as e:
                ctx.log.error(f"ERROR: Failed to parse host data from Vault: {e}")
                plugin_state["hosts"] = []
        else:
            ctx.log.info("INFO: No hosts found in Vault. Starting with empty list.")
            plugin_state["hosts"] = []

    def save_state_to_vault():
        try:
            success = ctx.set_secret("configured_hosts", json.dumps(plugin_state["hosts"]))
            if success:
                ctx.log.debug("SUCCESS: Host list saved to Vault.")
        except Exception as e:
            ctx.log.error(f"ERROR: Vault Save Error: {e}", exc_info=True)


    # --- SEITE 1: HAUPTANSICHT ---
    @ui.page('/docker')
    @main_layout('Docker Nodes')
    async def docker_page(): 
        with ui.row().classes('w-full justify-between items-center mb-6'):
            ui.label('Docker Swarm / Live Status').classes('text-2xl font-bold dark:text-zinc-100')
            
            with ui.row().classes('gap-4'):
                ui.button('Einstellungen', icon='settings', on_click=lambda: ui.navigate.to('/docker/settings')).props('unelevated outline rounded size=sm color=slate')
                refresh_btn = ui.button('Refresh All', icon='sync', color='primary').props('unelevated outline rounded size=sm')

        container_wrapper = ui.column().classes('w-full gap-4')

        async def refresh_containers():
            if not plugin_state["hosts"]:
                ui.notify("Keine Docker Hosts konfiguriert. Bitte erst in den Einstellungen hinzufügen.", type="warning")
                return

            refresh_btn.props('loading')
            live_data_dict = await run.io_bound(fetch_all_containers_parallel)
            container_wrapper.clear()
            
            with container_wrapper:
                for host_name, conts in live_data_dict.items():
                    total = len(conts)
                    
                    if total > 0 and conts[0].get('state') == 'error':
                        header_text = f"{host_name} (OFFLINE)"
                        header_color = '!bg-red-50 dark:!bg-red-900/10 text-red-600 dark:text-red-400'
                    else:
                        running = sum(1 for c in conts if c['state'] == 'running')
                        header_text = f"{host_name} ({running}/{total} Running)"
                        header_color = '!bg-white dark:!bg-zinc-900 text-slate-800 dark:text-zinc-200'

                    with ui.expansion(header_text, icon='dns').classes(f'w-full shadow-sm border border-slate-200 dark:border-zinc-800 rounded-2xl {header_color} overflow-hidden'):
                        cont_columns = [
                            {'name': 'name', 'label': 'Container Name', 'field': 'name', 'align': 'left', 'sortable': True},
                            {'name': 'image', 'label': 'Image', 'field': 'image', 'align': 'left'},
                            {'name': 'state', 'label': 'State', 'field': 'state', 'align': 'left'},
                            {'name': 'status', 'label': 'Status Info', 'field': 'status', 'align': 'left'},
                        ]
                        
                        inner_table = ui.table(columns=cont_columns, rows=conts, row_key='id').classes('w-full no-shadow !bg-transparent')
                        inner_table.add_slot('body-cell-state', '''
                            <q-td :props="props">
                                <q-chip :color="props.value === 'running' ? 'positive' : (props.value === 'error' ? 'orange' : 'negative')" text-color="white" dense size="sm" class="font-bold uppercase tracking-wider">
                                    {{ props.value }}
                                </q-chip>
                            </q-td>
                        ''')

            refresh_btn.props(remove='loading')
            ui.notify(f'Update abgeschlossen: {sum(len(c) for c in live_data_dict.values())} Container gefunden.', type='positive')

        refresh_btn.on_click(refresh_containers)
        
        # Initialer Ladevorgang (Nur wenn Hosts da sind)
        if plugin_state["hosts"]:
            ui.timer(0.1, refresh_containers, once=True)
        else:
            with container_wrapper:
                ui.label("Aktuell sind keine Docker Hosts konfiguriert.").classes("text-zinc-500 italic mt-4")


    # --- SEITE 2: EINSTELLUNGEN ---
    @ui.page('/docker/settings')
    @main_layout('Docker Nodes') 
    async def docker_settings_page():
        
        with ui.row().classes('w-full justify-between items-center mb-6'):
            ui.label('Host Management').classes('text-2xl font-bold dark:text-zinc-100')
            ui.button('Zurück zur Übersicht', icon='arrow_back', on_click=lambda: ui.navigate.to('/docker')).props('unelevated outline rounded size=sm color=slate')

        current_host = {'id': None}

        with ui.row().classes('w-full gap-6 flex-col lg:flex-row flex-nowrap items-start mb-6'):
            
            with ui.card().classes('w-full lg:w-2/3 p-6 shadow-sm border border-slate-200 dark:border-zinc-800 !bg-white dark:!bg-zinc-900 rounded-3xl'):
                ui.label('Registered Endpoints').classes('text-lg font-bold mb-4 dark:text-zinc-200')
                host_columns = [
                    {'name': 'name', 'label': 'Host Name', 'field': 'name', 'align': 'left', 'sortable': True},
                    {'name': 'ip', 'label': 'IP Address', 'field': 'ip', 'align': 'left'},
                    {'name': 'port', 'label': 'Port', 'field': 'port', 'align': 'left'},
                ]
                host_table = ui.table(columns=host_columns, rows=plugin_state["hosts"], row_key='id', selection='single', pagination=15).classes('w-full no-shadow border dark:border-zinc-800')

            with ui.card().classes('w-full lg:w-1/3 p-6 shadow-sm border border-slate-200 dark:border-zinc-800 !bg-white dark:!bg-zinc-900 rounded-3xl'):
                form_title = ui.label('Add Docker Host').classes('text-lg font-bold mb-4 dark:text-zinc-200')
                
                with ui.column().classes('w-full gap-3'):
                    name_input = ui.input('Host Name').classes('w-full').props('outlined dense')
                    with ui.row().classes('w-full gap-3 flex-nowrap'):
                        ip_input = ui.input('IP Address').classes('w-2/3').props('outlined dense')
                        port_input = ui.number('Port', value=2375).classes('w-1/3').props('outlined dense')
                    ui.separator().classes('my-2 dark:bg-zinc-800')
                    
                    def clear_form():
                        current_host['id'] = None
                        form_title.set_text('Add Docker Host')
                        name_input.value = ''
                        ip_input.value = ''
                        port_input.value = 2375
                        btn_delete.set_visibility(False)
                        host_table.selected.clear()
                        host_table.update()

                    def handle_selection(e):
                        if not host_table.selected:
                            clear_form()
                            return
                        selected = host_table.selected[0]
                        current_host['id'] = selected['id']
                        form_title.set_text('Edit Docker Host')
                        name_input.value = selected.get('name', '')
                        ip_input.value = selected.get('ip', '')
                        port_input.value = selected.get('port', 2375)
                        btn_delete.set_visibility(True)

                    def save_host():
                        if not name_input.value or not ip_input.value:
                            ui.notify('Name und IP sind Pflichtfelder!', type='negative')
                            return
                        
                        if current_host['id'] is not None:
                            for h in plugin_state["hosts"]:
                                if h['id'] == current_host['id']:
                                    h.update({'name': name_input.value, 'ip': ip_input.value, 'port': port_input.value})
                                    break
                            ui.notify('Host aktualisiert!', type='positive', color='emerald')
                        else:
                            new_id = max([h['id'] for h in plugin_state["hosts"]] + [0]) + 1 if plugin_state["hosts"] else 1
                            plugin_state["hosts"].append({'id': new_id, 'name': name_input.value, 'ip': ip_input.value, 'port': port_input.value})
                            ui.notify('Host hinzugefügt!', type='positive', color='emerald')
                        
                        save_state_to_vault()
                        host_table.rows = plugin_state["hosts"]
                        host_table.update()
                        clear_form()

                    def delete_host():
                        if current_host['id'] is not None:
                            host_to_delete = next((h for h in plugin_state["hosts"] if h['id'] == current_host['id']), None)
                            if host_to_delete:
                                plugin_state["hosts"].remove(host_to_delete)
                                ui.notify(f"Host {host_to_delete['name']} entfernt.", type='info')
                                save_state_to_vault()
                                host_table.rows = plugin_state["hosts"]
                                host_table.update()
                                clear_form()

                    host_table.on('selection', handle_selection)
       
                    
                    with ui.row().classes('w-full justify-between items-center'):
                        btn_delete = ui.button(icon='delete', on_click=delete_host, color='red').props('unelevated rounded flat').classes('px-2')
                        btn_delete.set_visibility(False) 
                        with ui.row().classes('gap-2'):
                            ui.button('Cancel', on_click=clear_form, color='slate').props('unelevated rounded')
                            ui.button('Save', on_click=save_host, color='primary').props('unelevated rounded')