import streamlit as st
import pandas as pd
import numpy as np
import plotly.graph_objects as go
from plotly.subplots import make_subplots
import os

# ==========================================
# 1. KONFIGURASI HALAMAN & CUSTOM CSS
# ==========================================
st.set_page_config(
    page_title="Dashboard SIRAPI - Kurva S",
    page_icon="📈",
    layout="wide",
    initial_sidebar_state="expanded"
)

# Custom CSS untuk UI yang lebih Profesional
st.markdown("""
    <style>
    /* Styling Dashboard Title */
    .main-title { font-size: 32px; font-weight: 800; color: #0F172A; margin-bottom: 5px; }
    .sub-title { font-size: 16px; color: #64748B; margin-bottom: 30px; }
    
    /* Styling Metrics (KPI Cards) */
    div[data-testid="metric-container"] {
        background-color: #ffffff;
        border: 1px solid #E2E8F0;
        padding: 15px 20px;
        border-radius: 10px;
        box-shadow: 0 4px 6px -1px rgba(0, 0, 0, 0.05);
        border-left: 5px solid #2563EB;
    }
    div[data-testid="metric-container"] > label { font-size: 14px; color: #64748B; font-weight: 600; }
    div[data-testid="metric-container"] > div > div { font-size: 28px; font-weight: bold; color: #1E293B; }
    
    /* Tabel Styling */
    .stDataFrame { border-radius: 10px; overflow: hidden; box-shadow: 0 4px 6px -1px rgba(0,0,0,0.1); }
    </style>
""", unsafe_allow_html=True)

st.markdown('<div class="main-title">📈 Dashboard Monitoring PM & Kurva S (SIRAPI)</div>', unsafe_allow_html=True)
st.markdown('<div class="sub-title">Integrasi data Preventive Maintenance (Site & Genset) dengan visualisasi progres otomatis.</div>', unsafe_allow_html=True)


# ==========================================
# 2. FUNGSI PENGOLAH DATA KURVA S
# ==========================================
@st.cache_data(show_spinner=False)
def calculate_scurve(df_filtered, completed_statuses):
    df_calc = df_filtered.copy()
    df_calc['Schedule Date'] = pd.to_datetime(df_calc['Schedule Date'], errors='coerce')
    df_calc['Submitted Date'] = pd.to_datetime(df_calc['Submitted Date'], errors='coerce')
    
    # Drop data tanpa tanggal schedule
    df_calc = df_calc.dropna(subset=['Schedule Date'])
    
    total_sites = len(df_calc)
    if total_sites == 0:
        return None, 0, 0, 0, 0

    weight_per_site = 100.0 / total_sites
    
    min_sched = df_calc['Schedule Date'].min()
    max_sched = df_calc['Schedule Date'].max()
    
    # Filter status selesai berdasarkan input pengguna (Submitted / + Waiting Approval)
    df_calc['Status_Clean'] = df_calc['Status'].astype(str).str.upper().str.strip()
    df_submitted = df_calc[df_calc['Status_Clean'].isin(completed_statuses)]
    completed_sites = len(df_submitted)
    
    if not df_submitted.empty:
        min_sub = df_submitted['Submitted Date'].min()
        max_sub = df_submitted['Submitted Date'].max()
        start_date = min(min_sched, min_sub)
        end_date = max(max_sched, max_sub)
    else:
        start_date = min_sched
        end_date = max_sched
        max_sub = None
        
    all_dates = pd.date_range(start=start_date, end=end_date, freq='D')
    timeline_df = pd.DataFrame({'Date': all_dates})
    
    # Target Daily & Kumulatif
    target_daily = df_calc.groupby('Schedule Date').size().reset_index(name='Target_Unit')
    timeline_df = pd.merge(timeline_df, target_daily, left_on='Date', right_on='Schedule Date', how='left')
    timeline_df['Target_Unit'] = timeline_df['Target_Unit'].fillna(0)
    timeline_df['Target_Bobot'] = timeline_df['Target_Unit'] * weight_per_site
    timeline_df['Target_Kumulatif'] = timeline_df['Target_Bobot'].cumsum()
    
    # Realisasi Daily & Kumulatif
    if not df_submitted.empty:
        actual_daily = df_submitted.groupby('Submitted Date').size().reset_index(name='Actual_Unit')
        timeline_df = pd.merge(timeline_df, actual_daily, left_on='Date', right_on='Submitted Date', how='left')
        timeline_df['Actual_Unit'] = timeline_df['Actual_Unit'].fillna(0)
        timeline_df['Actual_Bobot'] = timeline_df['Actual_Unit'] * weight_per_site
        timeline_df['Actual_Kumulatif'] = timeline_df['Actual_Bobot'].cumsum()
        
        # Plot garis realisasi hanya sampai tanggal cutoff terakhir
        timeline_df['Actual_Kumulatif_Plot'] = timeline_df.apply(
            lambda r: r['Actual_Kumulatif'] if r['Date'] <= max_sub else np.nan, axis=1
        )
        
        # Ambil nilai aktual terakhir
        current_actual_pct = timeline_df.loc[timeline_df['Date'] == max_sub, 'Actual_Kumulatif'].values[0]
        # Deviasi = Realisasi Hari Ini - Target Hari Ini (pada tanggal realisasi terakhir)
        current_target_pct = timeline_df.loc[timeline_df['Date'] == max_sub, 'Target_Kumulatif'].values[0]
        deviation = current_actual_pct - current_target_pct
    else:
        timeline_df['Actual_Unit'] = 0
        timeline_df['Actual_Bobot'] = 0.0
        timeline_df['Actual_Kumulatif'] = 0.0
        timeline_df['Actual_Kumulatif_Plot'] = np.nan
        current_actual_pct = 0.0
        deviation = 0.0

    # Kalkulasi Deviasi harian untuk tabel
    timeline_df['Deviasi_Harian'] = timeline_df['Actual_Kumulatif'] - timeline_df['Target_Kumulatif']

    return timeline_df, total_sites, completed_sites, current_actual_pct, deviation


# ==========================================
# 3. SIDEBAR: UPLOAD & PENGATURAN LOGIKA
# ==========================================
st.sidebar.image("https://cdn-icons-png.flaticon.com/512/3256/3256114.png", width=60)
st.sidebar.header("📂 Data Source")

# Multi-file uploader
uploaded_files = st.sidebar.file_uploader(
    "Upload File Excel PM (Bisa >1 file)", 
    type=["xlsx", "xls"],
    accept_multiple_files=True
)

if uploaded_files:
    try:
        # Menggabungkan semua file yang diupload
        dfs = []
        for file in uploaded_files:
            df_temp = pd.read_excel(file)
            
            # Penamaan Tipe PM berdasarkan nama file (Otomatis deteksi Site / Genset)
            filename_lower = file.name.lower()
            if "genset" in filename_lower:
                df_temp['Tipe Data'] = "PM Genset"
            elif "site" in filename_lower:
                df_temp['Tipe Data'] = "PM Site"
            else:
                df_temp['Tipe Data'] = f"File: {file.name}"
                
            dfs.append(df_temp)
            
        df_raw = pd.concat(dfs, ignore_index=True)
        
        # Validasi Kolom Minimum
        required_cols = ['Schedule Date', 'Status', 'NOP']
        missing_cols = [col for col in required_cols if col not in df_raw.columns]
        if missing_cols:
            st.error(f"❌ File tidak valid! Kekurangan kolom dasar: {missing_cols}")
            st.stop()

        # ==========================================
        # 4. SIDEBAR: FILTER DATA
        # ==========================================
        st.sidebar.markdown("---")
        st.sidebar.header("🔍 Filter Parameter")
        
        # 4A. Filter Tipe Data (Keseluruhan / PM Site / PM Genset)
        list_tipe = ["Gabungan (Semua Data)"] + sorted(list(df_raw['Tipe Data'].unique()))
        selected_tipe = st.sidebar.selectbox("Pilih Ruang Lingkup:", list_tipe)
        
        # 4B. Filter NOP
        list_nop = ["Semua NOP"] + sorted([str(x) for x in df_raw['NOP'].dropna().unique()])
        selected_nop = st.sidebar.selectbox("Pilih NOP:", list_nop)
        
        # 4C. Filter Cluster (Jika ada)
        if 'Cluster' in df_raw.columns:
            list_cluster = ["Semua Cluster"] + sorted([str(x) for x in df_raw['Cluster'].dropna().unique()])
            selected_cluster = st.sidebar.selectbox("Pilih Cluster:", list_cluster)
        else:
            selected_cluster = "Semua Cluster"

        # 4D. Pengaturan Status (Fitur Waiting Approval)
        st.sidebar.markdown("---")
        st.sidebar.header("⚙️ Konfigurasi Perhitungan")
        include_wa = st.sidebar.checkbox(
            "Hitung 'WAITING APPROVAL' sebagai Selesai?", 
            value=True, 
            help="Jika dicentang, tiket dengan status Waiting Approval akan dianggap sudah realisasi pada Kurva S. Hilangkan centang jika status ini berpotensi Takeout."
        )
        
        # Logika penetapan status selesai
        completed_statuses = ['SUBMITTED']
        if include_wa:
            completed_statuses.append('WAITING APPROVAL')


        # ==========================================
        # 5. PROSES FILTERING
        # ==========================================
        df_filtered = df_raw.copy()
        
        if selected_tipe != "Gabungan (Semua Data)":
            df_filtered = df_filtered[df_filtered['Tipe Data'] == selected_tipe]
        if selected_nop != "Semua NOP":
            df_filtered = df_filtered[df_filtered['NOP'] == selected_nop]
        if selected_cluster != "Semua Cluster":
            df_filtered = df_filtered[df_filtered['Cluster'] == selected_cluster]


        # ==========================================
        # 6. TAMPILAN DASHBOARD UTAMA
        # ==========================================
        timeline_df, total_sites, completed_sites, current_actual_pct, deviation = calculate_scurve(df_filtered, completed_statuses)

        if timeline_df is not None and total_sites > 0:
            
            # --- SUMMARY KECIL PER TIPE DATA ---
            st.markdown("##### 📌 Rekap Total Data Ter-upload:")
            summary_df = df_raw.groupby('Tipe Data').size().reset_index(name='Total Tiket')
            st.dataframe(summary_df.T, header=False, use_container_width=True)
            st.markdown("<br>", unsafe_allow_html=True)
            
            # --- METRIC CARDS ---
            col1, col2, col3, col4 = st.columns(4)
            col1.metric("🎯 Total Target Ticket", f"{total_sites:,} Unit")
            col2.metric("✅ Ticket Selesai", f"{completed_sites:,} Unit")
            col3.metric("📈 Progres Realisasi", f"{current_actual_pct:.2f}%")
            col4.metric(
                "⚖️ Deviasi (vs Target)", 
                f"{deviation:+.2f}%", 
                delta_color="normal" if deviation >= 0 else "inverse"
            )

            st.markdown("<br>", unsafe_allow_html=True)

            # --- PLOTLY S-CURVE (PROFESSIONAL LOOK) ---
            fig = make_subplots(specs=[[{"secondary_y": True}]])

            # Garis Target (Biru Tua)
            fig.add_trace(
                go.Scatter(
                    x=timeline_df['Date'], 
                    y=timeline_df['Target_Kumulatif'],
                    mode='lines',
                    name='Target Kumulatif (%)',
                    line=dict(color='#1E3A8A', width=4, dash='dot'),
                    hovertemplate="<b>Target:</b> %{y:.2f}%<extra></extra>"
                ),
                secondary_y=False
            )

            # Garis Aktual (Hijau Terang)
            fig.add_trace(
                go.Scatter(
                    x=timeline_df['Date'], 
                    y=timeline_df['Actual_Kumulatif_Plot'],
                    mode='lines+markers',
                    name='Realisasi Kumulatif (%)',
                    line=dict(color='#10B981', width=5),
                    marker=dict(size=8, symbol='circle', color='#10B981', line=dict(width=2, color='white')),
                    hovertemplate="<b>Realisasi:</b> %{y:.2f}%<extra></extra>"
                ),
                secondary_y=False
            )

            # Bar Chart Target Unit (Abu-abu / Light Blue)
            fig.add_trace(
                go.Bar(
                    x=timeline_df['Date'],
                    y=timeline_df['Target_Unit'],
                    name='Target Harian (Unit)',
                    opacity=0.3,
                    marker_color='#94A3B8',
                    hovertemplate="<b>Target Harian:</b> %{y} Unit<extra></extra>"
                ),
                secondary_y=True
            )

            # Formatting Layout
            wa_text = "Termasuk WA" if include_wa else "Tanpa WA"
            fig.update_layout(
                title=dict(
                    text=f"Kurva S Progres - {selected_tipe}<br><span style='font-size:14px;color:gray'>Filter: NOP ({selected_nop}) | Status Kalkulasi: Submitted & {wa_text}</span>",
                    font=dict(size=20, color='#0F172A'),
                    y=0.95
                ),
                hovermode="x unified",
                legend=dict(
                    orientation="h", yanchor="bottom", y=-0.2, xanchor="center", x=0.5,
                    bgcolor='rgba(255, 255, 255, 0.8)', bordercolor='#E2E8F0', borderwidth=1
                ),
                plot_bgcolor='rgba(0,0,0,0)',
                paper_bgcolor='rgba(0,0,0,0)',
                height=550,
                margin=dict(l=20, r=20, t=80, b=20)
            )

            # Styling Axes
            fig.update_xaxes(
                title_text="Timeline Pekerjaan", 
                tickformat="%d %b '%y", 
                showgrid=True, gridcolor='#F1F5F9', gridwidth=1,
                tickfont=dict(color='#64748B')
            )
            fig.update_yaxes(
                title_text="Progres Kumulatif (%)", 
                range=[0, 105], 
                showgrid=True, gridcolor='#E2E8F0', gridwidth=1,
                titlefont=dict(color='#1E3A8A', weight='bold'),
                tickfont=dict(color='#1E3A8A'),
                secondary_y=False
            )
            fig.update_yaxes(
                title_text="Jumlah Unit (Daily)", 
                showgrid=False, 
                titlefont=dict(color='#64748B'),
                tickfont=dict(color='#64748B'),
                secondary_y=True
            )

            st.plotly_chart(fig, use_container_width=True)

            # --- TABEL DATA & EXPORT ---
            st.markdown("### 📋 Rincian Data")
            tab1, tab2 = st.tabs(["📊 Tabel Rekap Harian Kurva S", "🗂️ Data Ticket Terfilter (Raw)"])
            
            with tab1:
                rekap_export = timeline_df[['Date', 'Target_Unit', 'Target_Kumulatif', 'Actual_Unit', 'Actual_Kumulatif', 'Deviasi_Harian']].copy()
                rekap_export.columns = ['Tanggal', 'Target Harian (Unit)', 'Target Kumulatif (%)', 'Realisasi Harian (Unit)', 'Realisasi Kumulatif (%)', 'Deviasi Harian (%)']
                
                # Format desimal agar rapi
                styled_rekap = rekap_export.style.format({
                    'Target Kumulatif (%)': '{:.2f}%',
                    'Realisasi Kumulatif (%)': '{:.2f}%',
                    'Deviasi Harian (%)': '{:+.2f}%'
                }).applymap(
                    lambda val: 'color: red; font-weight:bold;' if val < 0 else 'color: green; font-weight:bold;', 
                    subset=['Deviasi Harian (%)']
                )
                
                st.dataframe(styled_rekap, use_container_width=True)

            with tab2:
                st.dataframe(df_filtered, use_container_width=True)

        else:
            st.warning("⚠️ Data tidak ditemukan untuk kombinasi filter yang dipilih atau tidak ada tanggal schedule.")

    except Exception as e:
        st.error(f"❌ Terjadi kesalahan sistem: {e}")
        st.info("Pastikan format Excel Anda tidak *corrupt* dan kolom tanggal valid.")

else:
    # Tampilan Awal Landing Page
    st.info("👈 Silakan upload file Excel PM Anda pada sidebar (Bisa lebih dari 1 file sekaligus).")
    
    col_a, col_b = st.columns(2)
    with col_a:
        st.markdown("""
        ### 💡 Tips Penamaan File:
        Agar sistem otomatis mendeteksi apakah data tersebut PM Site atau PM Genset, beri nama file Anda seperti:
        * `Rekap_PM_Site_Agustus.xlsx`
        * `Data_PM_Genset_Q3.xlsx`
        """)
    with col_b:
        st.markdown("""
        ### 📋 Syarat Kolom Minimal:
        * **NOP** (Area/Wilayah)
        * **Schedule Date** (Tanggal Rencana)
        * **Submitted Date** (Tanggal Aktual Selesai)
        * **Status** (SUBMITTED, WAITING APPROVAL, dll)
        """)
