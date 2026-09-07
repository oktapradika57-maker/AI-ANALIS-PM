import streamlit as st
import pandas as pd
import numpy as np
import plotly.graph_objects as go
from plotly.subplots import make_subplots

# ==========================================
# 1. KONFIGURASI HALAMAN & CSS STYLING
# ==========================================
st.set_page_config(
    page_title="Dashboard SIRAPI - PM Monitoring",
    page_icon="📈",
    layout="wide",
    initial_sidebar_state="expanded"
)

# Custom CSS untuk Tampilan Profesional
st.markdown("""
    <style>
    .main-header { font-size: 28px; font-weight: 800; color: #0f4c75; margin-bottom: -10px;}
    .sub-header { font-size: 16px; color: #6c757d; margin-bottom: 20px;}
    
    div[data-testid="metric-container"] {
        background-color: #ffffff;
        border: 1px solid #e0e0e0;
        padding: 15px 20px;
        border-radius: 10px;
        box-shadow: 2px 4px 10px rgba(0, 0, 0, 0.05);
        border-left: 5px solid #0f4c75;
    }
    div[data-testid="metric-container"] label {
        font-size: 14px;
        font-weight: 600;
        color: #333333;
    }
    .dataframe-container { margin-bottom: 30px; }
    </style>
""", unsafe_allow_html=True)

st.markdown('<div class="main-header">📈 Dashboard Monitoring Kurva S & Leaderboard (SIRAPI)</div>', unsafe_allow_html=True)
st.markdown('<div class="sub-header">Monitoring Preventive Maintenance (PM Site & PM Genset) Terintegrasi</div>', unsafe_allow_html=True)

# ==========================================
# 2. FUNGSI PENGOLAH DATA
# ==========================================
def calculate_scurve(df_filtered, completed_statuses):
    df_filtered = df_filtered.copy()
    
    df_filtered['Schedule Date'] = pd.to_datetime(df_filtered['Schedule Date'], errors='coerce')
    df_filtered['Submitted Date'] = pd.to_datetime(df_filtered['Submitted Date'], errors='coerce')
    df_filtered = df_filtered.dropna(subset=['Schedule Date'])
    
    total_sites = len(df_filtered)
    if total_sites == 0:
        return None, 0, 0, 0, 0

    weight_per_site = 100.0 / total_sites
    
    min_sched = df_filtered['Schedule Date'].min()
    max_sched = df_filtered['Schedule Date'].max()
    
    df_submitted = df_filtered[df_filtered['Status'].astype(str).str.upper().isin(completed_statuses)]
    completed_sites = len(df_submitted)
    
    if not df_submitted.empty:
        min_sub = df_submitted['Submitted Date'].min()
        max_sub = df_submitted['Submitted Date'].max()
        start_date = min(min_sched, min_sub) if pd.notnull(min_sub) else min_sched
        end_date = max(max_sched, max_sub) if pd.notnull(max_sub) else max_sched
    else:
        start_date = min_sched
        end_date = max_sched
        max_sub = None
        
    all_dates = pd.date_range(start=start_date, end=end_date, freq='D')
    timeline_df = pd.DataFrame({'Date': all_dates})
    
    target_daily = df_filtered.groupby('Schedule Date').size().reset_index(name='Target_Unit')
    timeline_df = pd.merge(timeline_df, target_daily, left_on='Date', right_on='Schedule Date', how='left')
    timeline_df['Target_Unit'] = timeline_df['Target_Unit'].fillna(0)
    timeline_df['Target_Kumulatif'] = (timeline_df['Target_Unit'] * weight_per_site).cumsum()
    
    if not df_submitted.empty:
        actual_daily = df_submitted.groupby('Submitted Date').size().reset_index(name='Actual_Unit')
        timeline_df = pd.merge(timeline_df, actual_daily, left_on='Date', right_on='Submitted Date', how='left')
        timeline_df['Actual_Unit'] = timeline_df['Actual_Unit'].fillna(0)
        timeline_df['Actual_Kumulatif'] = (timeline_df['Actual_Unit'] * weight_per_site).cumsum()
        
        timeline_df['Actual_Kumulatif_Plot'] = timeline_df.apply(
            lambda r: r['Actual_Kumulatif'] if r['Date'] <= max_sub else np.nan, axis=1
        )
        current_actual_pct = timeline_df.loc[timeline_df['Date'] == max_sub, 'Actual_Kumulatif'].values[0] if max_sub else 0
        current_target_pct = timeline_df.loc[timeline_df['Date'] == max_sub, 'Target_Kumulatif'].values[0] if max_sub else 0
        deviation = current_actual_pct - current_target_pct
    else:
        timeline_df['Actual_Unit'] = 0
        timeline_df['Actual_Kumulatif'] = 0.0
        timeline_df['Actual_Kumulatif_Plot'] = np.nan
        current_actual_pct = 0.0
        deviation = 0.0
        
    timeline_df['Deviasi'] = timeline_df['Actual_Kumulatif'] - timeline_df['Target_Kumulatif']
    return timeline_df, total_sites, completed_sites, current_actual_pct, deviation

def generate_leaderboard(df_filtered, completed_statuses):
    if df_filtered.empty:
        return pd.DataFrame()

    # Hitung Agregat Keseluruhan per NOP (Total dari seluruh NOP)
    agg_nop = df_filtered.groupby('NOP').agg(
        Count_Site=('NOP', 'count'),
        Total_Closed=('Status', lambda x: x.astype(str).str.upper().isin(completed_statuses).sum())
    ).reset_index()
    agg_nop['% Ach'] = (agg_nop['Total_Closed'] / agg_nop['Count_Site'] * 100).fillna(0)
    agg_nop['Rank'] = agg_nop['% Ach'].rank(method='min', ascending=False).astype(int)

    # Hitung Rincian
    agg_detail = df_filtered.groupby(['NOP', 'Sumber File']).agg(
        Count_Site=('NOP', 'count'),
        Total_Closed=('Status', lambda x: x.astype(str).str.upper().isin(completed_statuses).sum())
    ).reset_index()
    agg_detail['% Ach'] = (agg_detail['Total_Closed'] / agg_detail['Count_Site'] * 100).fillna(0)

    records = []
    grand_site = 0
    grand_closed = 0

    for _, row in agg_nop.sort_values('Rank').iterrows():
        nop_name = row['NOP']
        records.append({
            'NOP': f"⊟ {nop_name}",
            'PM Status': 'TOTAL NOP',
            'Count of Site': row['Count_Site'],
            'Total Closed': row['Total_Closed'],
            '% Ach': row['% Ach'] / 100.0,
            'Rank': row['Rank']
        })
        grand_site += row['Count_Site']
        grand_closed += row['Total_Closed']

        details = agg_detail[agg_detail['NOP'] == nop_name]
        for _, d_row in details.iterrows():
            tipe_pm = str(d_row['Sumber File']).replace('.xlsx', '').replace('.xls', '')
            records.append({
                'NOP': "", 
                'PM Status': tipe_pm,
                'Count of Site': d_row['Count_Site'],
                'Total Closed': d_row['Total_Closed'],
                '% Ach': d_row['% Ach'] / 100.0,
                'Rank': ""
            })

    grand_pct = (grand_closed / grand_site) if grand_site > 0 else 0
    records.append({
        'NOP': 'Grand Total',
        'PM Status': '',
        'Count of Site': grand_site,
        'Total Closed': grand_closed,
        '% Ach': grand_pct,
        'Rank': ''
    })

    return pd.DataFrame(records)

# ==========================================
# 3. SIDEBAR & FILE UPLOAD
# ==========================================
st.sidebar.image("https://cdn-icons-png.flaticon.com/512/3256/3256013.png", width=60)
st.sidebar.header("📁 Upload Data")

uploaded_files = st.sidebar.file_uploader(
    "Upload File PM Site & PM Genset (Excel)", 
    type=["xlsx", "xls"], 
    accept_multiple_files=True
)

if uploaded_files:
    try:
        df_list = []
        for file in uploaded_files:
            temp_df = pd.read_excel(file)
            temp_df['Sumber File'] = file.name 
            df_list.append(temp_df)
            
        df_raw = pd.concat(df_list, ignore_index=True)
        
        required_cols = ['Schedule Date', 'Submitted Date', 'Status', 'NOP']
        missing_cols = [col for col in required_cols if col not in df_raw.columns]
        if missing_cols:
            st.error(f"File tidak valid. Kurang kolom berikut: {missing_cols}")
            st.stop()

        all_unique_statuses = sorted(df_raw['Status'].dropna().astype(str).str.upper().unique().tolist())

        # ==========================================
        # 4. FILTER PARAMETER
        # ==========================================
        st.sidebar.markdown("---")
        st.sidebar.header("🔍 Pengaturan Logika Kurva")
        
        default_completed = [s for s in ['SUBMITTED', 'CLOSED', 'DONE', 'APPROVED'] if s in all_unique_statuses]
        if not default_completed and all_unique_statuses:
            default_completed = [all_unique_statuses[0]]
            
        selected_completed = st.sidebar.multiselect(
            "1. Status Dihitung Selesai (Actual):",
            options=all_unique_statuses,
            default=default_completed
        )
        
        include_waiting = st.sidebar.checkbox("2. Hitung 'Waiting Approval' sbg Target", value=True)

        st.sidebar.markdown("---")
        st.sidebar.header("🎯 Filter Area & Tipe")
        
        list_sumber = sorted(df_raw['Sumber File'].unique().tolist())
        selected_sumber = st.sidebar.multiselect("Tipe PM (Sumber File):", list_sumber, default=list_sumber)

        list_nop = sorted([str(x) for x in df_raw['NOP'].dropna().unique()])
        selected_nop = st.sidebar.multiselect("Pilih NOP:", list_nop, default=list_nop)
        
        if 'Cluster' in df_raw.columns:
            list_cluster = ["Semua Cluster"] + sorted([str(x) for x in df_raw['Cluster'].dropna().unique()])
            selected_cluster = st.sidebar.selectbox("Pilih Cluster:", list_cluster)
        else:
            selected_cluster = "Semua Cluster"

        # --- PROSES FILTERING ---
        df_filtered = df_raw.copy()
        
        if selected_sumber:
            df_filtered = df_filtered[df_filtered['Sumber File'].isin(selected_sumber)]
            
        if selected_nop:
            df_filtered = df_filtered[df_filtered['NOP'].isin(selected_nop)]
            
        if selected_cluster != "Semua Cluster":
            df_filtered = df_filtered[df_filtered['Cluster'] == selected_cluster]
            
        if not include_waiting:
            df_filtered = df_filtered[~df_filtered['Status'].astype(str).str.upper().str.contains('WAITING APPROVAL')]

        # ==========================================
        # 5. PERHITUNGAN & TAMPILAN DASHBOARD
        # ==========================================
        timeline_df, total_sites, completed_sites, current_actual_pct, deviation = calculate_scurve(df_filtered, selected_completed)

        if timeline_df is not None:
            # --- LOGIKA PENAMAAN DINAMIS (SITE / GENSET / GABUNGAN) ---
            sumber_unik = df_filtered['Sumber File'].astype(str).str.upper().unique()
            is_pms = any('SITE' in s or 'PMS' in s for s in sumber_unik)
            is_pmg = any('GENSET' in s or 'PMG' in s for s in sumber_unik)
            
            if is_pms and is_pmg:
                label_target = "Total Target Site & Genset"
                label_actual = "Site & Genset Terealisasi"
                unit_text = "Site & Genset"
            elif is_pms:
                label_target = "Total Target Site"
                label_actual = "Site Terealisasi"
                unit_text = "Site"
            elif is_pmg:
                label_target = "Total Target Genset"
                label_actual = "Genset Terealisasi"
                unit_text = "Genset"
            else:
                # Default jika penamaan file tidak mengandung unsur site/genset/pms/pmg
                if len(sumber_unik) > 1:
                    label_target = "Total Target Site & Genset"
                    label_actual = "Site & Genset Terealisasi"
                    unit_text = "Site & Genset"
                else:
                    label_target = "Total Target"
                    label_actual = "Terealisasi"
                    unit_text = "Unit"

            # --- KPI Cards Dinamis ---
            col1, col2, col3, col4 = st.columns(4)
            col1.metric(f"📌 {label_target}", f"{total_sites:,.0f} {unit_text}")
            col2.metric(f"✅ {label_actual}", f"{completed_sites:,.0f} {unit_text}")
            col3.metric("📈 Progres Realisasi", f"{current_actual_pct:.2f}%")
            col4.metric("⚖️ Deviasi", f"{deviation:+.2f}%", delta_color="normal")
            
            st.markdown("<hr style='margin: 10px 0px 25px 0px;'>", unsafe_allow_html=True)

            # --- TABEL LEADERBOARD ---
            st.markdown("### 🏆 Peringkat Pencapaian per NOP")
            
            df_leaderboard = generate_leaderboard(df_filtered, selected_completed)
            
            def style_leaderboard(row):
                if row['NOP'] == 'Grand Total':
                    return ['background-color: #d9e1f2; font-weight: bold; color: black; border-top: 2px solid #0f4c75;'] * len(row)
                elif row['PM Status'] == 'TOTAL NOP':
                    return ['background-color: #f8f9fa; font-weight: bold; color: #0f4c75; border-top: 1px solid #dee2e6;'] * len(row)
                else:
                    return ['background-color: #ffffff; color: #495057;'] * len(row)

            if not df_leaderboard.empty:
                styled_df = df_leaderboard.style.apply(style_leaderboard, axis=1).format({
                    "% Ach": "{:.0%}"
                })
                st.dataframe(styled_df, use_container_width=True, hide_index=True, height=int(35.2 * (len(df_leaderboard) + 1)))

            st.markdown("<br>", unsafe_allow_html=True)

            # --- GRAFIK KURVA S ---
            fig = make_subplots(specs=[[{"secondary_y": True}]])

            fig.add_trace(
                go.Bar(
                    x=timeline_df['Date'], y=timeline_df['Target_Unit'],
                    name=f'Target Harian ({unit_text})', opacity=0.3, marker_color='#cbd5e1', hoverinfo='x+y'
                ), secondary_y=True
            )

            fig.add_trace(
                go.Scatter(
                    x=timeline_df['Date'], y=timeline_df['Target_Kumulatif'],
                    mode='lines', name='Plan Kumulatif (%)', line=dict(color='#0f4c75', width=3, dash='dash'),
                ), secondary_y=False
            )

            fig.add_trace(
                go.Scatter(
                    x=timeline_df['Date'], y=timeline_df['Actual_Kumulatif_Plot'],
                    mode='lines+markers', name='Actual Kumulatif (%)',
                    line=dict(color='#2ca02c', width=4), marker=dict(size=6, color='#2ca02c'),
                    fill='tozeroy', fillcolor='rgba(44, 160, 44, 0.1)'
                ), secondary_y=False
            )
            
            # Dinamis Judul Grafik
            if len(sumber_unik) > 1:
                title_chart = "<b>S-Curve Gabungan (Monitoring PM)</b>"
            elif len(sumber_unik) == 1:
                title_chart = f"<b>S-Curve {sumber_unik[0]}</b>"
            else:
                title_chart = "<b>S-Curve (Monitoring PM)</b>"

            fig.update_layout(
                title=dict(text=title_chart, font=dict(size=20, color='#333333')),
                hovermode="x unified", legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1),
                height=500, margin=dict(l=40, r=40, t=60, b=40), plot_bgcolor='white', paper_bgcolor='white',
            )

            fig.update_xaxes(title_text="", tickformat="%d %b '%y", showgrid=True, gridcolor='#f1f5f9', linecolor='#cbd5e1')
            fig.update_yaxes(title_text="Progres Kumulatif (%)", range=[0, 105], showgrid=True, gridcolor='#f1f5f9', linecolor='#cbd5e1', secondary_y=False)
            fig.update_yaxes(title_text=f"Volume ({unit_text})", showgrid=False, secondary_y=True)

            st.plotly_chart(fig, use_container_width=True)

            # --- TABEL RAW DATA ---
            st.markdown("### 📋 Detail Data Ticket PM")
            tab1, tab2 = st.tabs(["📊 Tabel Rekap Harian (Kurva S)", "🗃️ Raw Data Ticket Terfilter"])
            
            with tab1:
                rekap_export = timeline_df[['Date', 'Target_Unit', 'Target_Kumulatif', 'Actual_Unit', 'Actual_Kumulatif', 'Deviasi']].copy()
                rekap_export.columns = ['Tanggal', f'Target Harian ({unit_text})', 'Target Kumulatif (%)', f'Realisasi Harian ({unit_text})', 'Realisasi Kumulatif (%)', 'Deviasi (%)']
                st.dataframe(rekap_export.style.format({
                    'Target Kumulatif (%)': '{:.2f}%', 'Realisasi Kumulatif (%)': '{:.2f}%', 'Deviasi (%)': '{:.2f}%'
                }), use_container_width=True)
            
            with tab2:
                st.dataframe(df_filtered, use_container_width=True)

        else:
            st.warning("⚠️ Data tidak ditemukan. Silakan cek kembali filter NOP, Status, atau Rentang Tanggal.")

    except Exception as e:
        st.error(f"❌ Terjadi kesalahan saat memproses file: {e}")

else:
    st.info("👈 Silakan upload file Excel PM Site dan/atau PM Genset Anda pada sidebar sebelah kiri untuk memulai.")
