"""
中国省市县分级地图绘制脚本

功能：读取 CSV 数据，合并省市县三级地理数据，绘制中国分级地图（含南海插图）。
支持线性色阶和对数色阶两种模式。

使用方法：
    1. 运行：uv run python draw_map.py [省|市|县]
    2. 在对应的 CSV 中填好 index_value 数据
"""

import sys
from dataclasses import dataclass, field
from typing import Optional

import geopandas as gpd
import matplotlib.pyplot as plt
import matplotlib.font_manager as fm
import cartopy.crs as ccrs
from cartopy.mpl.gridliner import LONGITUDE_FORMATTER, LATITUDE_FORMATTER
from matplotlib.colors import LinearSegmentedColormap, Normalize, LogNorm
from matplotlib.colorbar import ColorbarBase
from matplotlib.axes import Axes
from matplotlib.patches import Patch
import pandas as pd


# ====================================================================
# 配置（改数据只需修改 MapConfig）
# ====================================================================

@dataclass
class MapConfig:
    """地图绘制配置"""
    # --- 核心选择 ---
    mode: str = '省'                          # 省 / 市 / 县
    scale: str = 'linear'                    # linear（线性）/ log（对数）
    n_top: int = 30                          # 标注前 N 个高值区域

    # --- 颜色（想换色时取消注释改这里）---
    colors: list = field(default_factory=lambda: [
        '#ffffcc', '#c2e699', '#78c679', '#31a354', '#006837',
    ])

    # --- 数值范围（None = 自动从数据推断）---
    value_min: Optional[float] = None
    value_max: Optional[float] = None

    # --- 对数色阶刻度（仅 scale='log' 时生效）---
    log_ticks: list = field(default_factory=lambda: [200, 500, 1000, 2000, 5000, 10000])

    # --- 地图地理参数（一般不动）---
    gis_dir: str = "gis_data"
    map_extent: list = field(default_factory=lambda: [78.5, 135, 17, 53])
    inset_extent: list = field(default_factory=lambda: [105, 122, 2, 25])
    figsize: tuple = (20, 14)
    dpi: int = 300

    # --- 颜色（一般不动）---
    no_data_color: str = '#d9d9d9'
    no_data_edge: str = '#aaaaaa'
    boundary_color: str = '#000000'
    coastline_color: str = '#2C5F8A'
    ten_line_color: str = '#060d1b'

    # --- 标签与输出（None 则使用模式默认值）---
    label: str = 'value'
    title: Optional[str] = None               # 图表标题，如 '省级地图'
    output: Optional[str] = None              # 输出文件名，如 '省级地图.png'


# ---- 创建配置实例 ----
config = MapConfig()

# ---- 命令行参数覆盖模式 ----
if len(sys.argv) > 1 and sys.argv[1] in ('省', '市', '县'):
    config.mode = sys.argv[1]


# ---- 模式→文件映射 ----
_MODE_FILES = {
    '省': {'shapefile': '中国_省.shp', 'data': 'province_data.csv',
           'title': '省级地图', 'output': '省级地图.png'},
    '市': {'shapefile': '中国_市.shp', 'data': 'city_data.csv',
           'title': '地级市地图', 'output': '地级市地图.png'},
    '县': {'shapefile': '中国_县.shp', 'data': 'county_data.csv',
           'title': '县级地图', 'output': '县级地图.png'},
}
_cfg = _MODE_FILES[config.mode]


# ====================================================================
# 内部数据结构
# ====================================================================

@dataclass
class _MapData:
    """保存已加载和处理的地理数据"""
    regions: gpd.GeoDataFrame
    ten_lines: gpd.GeoDataFrame
    land_boundary: gpd.GeoDataFrame
    coastline: gpd.GeoDataFrame

    value_col: str
    vmin: float
    vmax: float

    cmap: LinearSegmentedColormap
    norm: Normalize | LogNorm
    no_data_color: str
    no_data_edge: str


@dataclass
class _PreparedMapData:
    """投影转换并缓冲后的地图数据"""
    regions_proj: gpd.GeoDataFrame
    regions_data: gpd.GeoDataFrame
    regions_nodata: gpd.GeoDataFrame
    ten_lines_proj: gpd.GeoDataFrame
    land_boundary_proj: gpd.GeoDataFrame
    coastline_proj: gpd.GeoDataFrame
    ten_line_buffers: list
    land_buffers: list
    regions_zero: gpd.GeoDataFrame


# ====================================================================
# 工具函数
# ====================================================================

def _setup_chinese_font() -> Optional[str]:
    """设置中文字体。"""
    zh_fonts = ['SimHei', 'Microsoft YaHei', 'SimSun',
                'Noto Sans CJK SC', 'Source Han Sans CN']
    for name in zh_fonts:
        try:
            fm.findfont(name, fallback_to_default=False)
            plt.rcParams['font.sans-serif'] = [name] + plt.rcParams['font.sans-serif']
            plt.rcParams['axes.unicode_minus'] = False
            print(f"使用中文字体: {name}")
            return name
        except Exception:
            continue
    print("未找到中文字体，中文可能显示为方框")
    return None


def _load_csv(path: str, value_col: str) -> pd.DataFrame:
    """读取并过滤 CSV（仅保留正值）。"""
    df = pd.read_csv(path, encoding='utf-8-sig')
    df = df[df[value_col] >= 0].reset_index(drop=True)
    print(f"有效数据: {len(df)} 条")
    if df.empty:
        print("警告：过滤后无有效数据！")
    return df


def _load_gis(gis_dir: str, shapefile: str):
    """加载地理边界数据。"""
    regions = gpd.read_file(f"{gis_dir}/{shapefile}")
    ten_lines = gpd.read_file(f"{gis_dir}/十段线_换向.shp")
    land_boundary = gpd.read_file(f"{gis_dir}/陆地边界.shp")
    coastline = gpd.read_file(f"{gis_dir}/海界.shp")
    print(f"  地理数据: {shapefile} ({len(regions)} 区域)")
    return regions, ten_lines, land_boundary, coastline


def _build_colormap(values: pd.Series, cfg: MapConfig
                     ) -> tuple[LinearSegmentedColormap, Normalize | LogNorm, float, float]:
    """根据数据和色阶模式构建颜色映射。"""
    data_min = values.min() if cfg.value_min is None else cfg.value_min
    data_max = values.max() if cfg.value_max is None else cfg.value_max
    print(f"数值范围: {data_min:.0f} ~ {data_max:.0f}")

    vmin = data_min if cfg.scale != 'log' else max(data_min, 1.0)
    vmax = data_max

    if cfg.scale == 'log':
        print(f"对数色阶: {vmin:.0f} ~ {vmax:.0f}")
        norm: Normalize | LogNorm = LogNorm(vmin=vmin, vmax=vmax)
    else:
        norm = Normalize(vmin=vmin, vmax=vmax)

    cmap = LinearSegmentedColormap.from_list('cmap', cfg.colors, N=256)
    return cmap, norm, vmin, vmax


def _compute_buffers(gdf: gpd.GeoDataFrame, distances: list[float], crs
                     ) -> list[gpd.GeoDataFrame]:
    """为同一 GeoDataFrame 计算多层缓冲区（共用一次投影）。"""
    proj = gdf.to_crs(crs)
    return [
        gpd.GeoDataFrame(geometry=proj.geometry.buffer(d, single_sided=True), crs=crs)
        for d in distances
    ]


def _prepare_map(map_data: _MapData, proj: ccrs.Projection) -> _PreparedMapData:
    """投影转换 + 缓冲区计算。"""
    regions_proj = map_data.regions.to_crs(proj)
    regions_data = regions_proj[regions_proj[map_data.value_col] > 0]
    regions_nodata = regions_proj[regions_proj[map_data.value_col] == -1]
    regions_zero = regions_proj[regions_proj[map_data.value_col] == 0]

    ten_lines_proj = map_data.ten_lines.to_crs(proj)
    land_boundary_proj = map_data.land_boundary.to_crs(proj)
    coastline_proj = map_data.coastline.to_crs(proj)

    ten_line_buffers = _compute_buffers(ten_lines_proj, [-20000, -40000], proj)

    simplified_land = land_boundary_proj.copy()
    simplified_land.geometry = simplified_land.geometry.simplify(
        tolerance=1000, preserve_topology=True)
    land_buffers = _compute_buffers(simplified_land, [20000, 40000], proj)

    return _PreparedMapData(
        regions_proj, regions_data, regions_nodata,
        ten_lines_proj, land_boundary_proj, coastline_proj,
        ten_line_buffers, land_buffers, regions_zero,
    )


# ====================================================================
# 绘图函数
# ====================================================================

def _draw_regions(ax: Axes, md: _MapData, prep: _PreparedMapData) -> None:
    """绘制所有区域（有数据 + 零值 + 缺失）。"""
    # -1（缺失）→ 白色底 + 斜线
    if not prep.regions_nodata.empty:
        prep.regions_nodata.plot(
            ax=ax, color='white', edgecolor=md.no_data_edge,
            hatch='///', linewidth=0.15, alpha=0.7, zorder=3)
    # 0 → 灰色
    if not prep.regions_zero.empty:
        prep.regions_zero.plot(
            ax=ax, color=md.no_data_color, edgecolor=md.no_data_edge,
            linewidth=0.15, alpha=0.85, zorder=3)
    # > 0 → 色阶
    if not prep.regions_data.empty:
        prep.regions_data.plot(
            ax=ax, column=md.value_col, cmap=md.cmap,
            edgecolor='#555555', linewidth=0.15, alpha=0.85,
            legend=False, vmin=md.vmin, vmax=md.vmax, norm=md.norm, zorder=4)


def _draw_boundaries(ax: Axes, prep: _PreparedMapData,
                     lw: tuple[float, float, float] = (1.2, 0.8, 1.5)) -> None:
    """绘制边界线（陆地边界、海岸线、十段线）。"""
    prep.land_boundary_proj.plot(ax=ax, color='#000000', linewidth=lw[0], zorder=6)
    prep.coastline_proj.plot(ax=ax, color='#2C5F8A', linewidth=lw[1], zorder=5)
    prep.ten_lines_proj.plot(ax=ax, color='#060d1b', linewidth=lw[2], linestyle='-')


def _draw_shadows(ax: Axes, prep: _PreparedMapData) -> None:
    """绘制十段线和陆界的阴影缓冲区。"""
    if len(prep.ten_line_buffers) >= 1:
        prep.ten_line_buffers[0].plot(ax=ax, color='#aeac8d', alpha=0.5, zorder=2)
    if len(prep.ten_line_buffers) >= 2:
        prep.ten_line_buffers[1].plot(ax=ax, color='#c4ace3', alpha=0.3, zorder=1)
    if len(prep.land_buffers) >= 1:
        prep.land_buffers[0].plot(ax=ax, color='#aeac8d', alpha=0.5, zorder=2)
    if len(prep.land_buffers) >= 2:
        prep.land_buffers[1].plot(ax=ax, color='#c4ace3', alpha=0.3, zorder=1)


def _annotate_top(ax: Axes, prep: _PreparedMapData, col: str, n: int) -> None:
    """标注数值前 N 的区域。"""
    top = prep.regions_data.nlargest(n, col)
    for centroid, val in zip(top.geometry.centroid, top[col]):
        ax.text(centroid.x, centroid.y, str(int(val)),
                transform=ax.projection, fontsize=7,
                ha='center', va='center', fontweight='bold', color='#1a1a1a', zorder=10)


# ====================================================================
# 主流程
# ====================================================================

def main() -> None:
    _setup_chinese_font()

    mode_files = _MODE_FILES[config.mode]
    title = config.title or mode_files['title']
    output = config.output or mode_files['output']
    print(f"当前模式: {config.mode}")
    print(f"  shapefile: {mode_files['shapefile']}")
    print(f"  CSV 数据: {mode_files['data']}")
    print(f"  输出文件: {output}")

    # 1. 读取数据
    df = _load_csv(mode_files['data'], 'index_value')

    # 2. 读取地理数据
    regions, ten_lines, land_boundary, coastline = _load_gis(
        config.gis_dir, mode_files['shapefile'])

    # 3. 合并数据
    merged = regions.merge(df, left_on='name', right_on='city_name', how='left')
    merged['index_value'] = merged['index_value'].fillna(-1)
    print(f"合并后区域数: {len(merged)}")
    has_data = (merged['index_value'] > 0).sum()
    is_zero = (merged['index_value'] == 0).sum()
    is_missing = (merged['index_value'] == -1).sum()
    print(f"有数据的区域数 (>0): {has_data}")
    print(f"零值区域数 (=0):  {is_zero}")
    print(f"缺失区域数 (=-1):  {is_missing}")

    # 4. 构建色阶
    valid = merged[merged['index_value'] > 0]['index_value']
    if valid.empty:
        print("错误：没有有效数据可绘制！")
        return
    cmap, norm, vmin, vmax = _build_colormap(valid, config)

    md = _MapData(
        regions=merged, ten_lines=ten_lines,
        land_boundary=land_boundary, coastline=coastline,
        value_col='index_value', vmin=vmin, vmax=vmax,
        cmap=cmap, norm=norm,
        no_data_color=config.no_data_color,
        no_data_edge=config.no_data_edge,
    )

    # 5. 投影
    crs = ccrs.AlbersEqualArea(
        central_longitude=105, central_latitude=0, standard_parallels=(25, 47))
    prep = _prepare_map(md, crs)

    # 6. 绘图
    fig = plt.figure(figsize=config.figsize)
    ax = fig.add_subplot(1, 1, 1, projection=crs)

    _draw_regions(ax, md, prep)
    _draw_boundaries(ax, prep)
    _draw_shadows(ax, prep)
    _annotate_top(ax, prep, 'index_value', config.n_top)

    # 7. 图例
    cax = fig.add_axes([0.06, 0.22, 0.015, 0.5])
    if config.scale == 'log':
        ticks = [t for t in config.log_ticks if vmin <= t <= vmax]
        cb = ColorbarBase(cax, cmap=cmap, norm=norm,
                          orientation='vertical', ticks=ticks, extend='both')
    else:
        cb = ColorbarBase(cax, cmap=cmap, norm=norm,
                          orientation='vertical', extend='max')
    cb.set_label(config.label, fontsize=12, fontweight='bold')
    cb.ax.yaxis.set_ticks_position('left')
    cb.ax.yaxis.set_label_position('left')
    cb.ax.tick_params(labelsize=10)

    # 图例：灰色 = 0，斜线 = 缺失
    legend_elements = [
        Patch(facecolor=config.no_data_color, edgecolor=config.no_data_edge,
              label='值为 0'),
        Patch(facecolor='white', edgecolor=config.no_data_edge, hatch='///',
              label='无数据'),
    ]
    ax.legend(handles=legend_elements, loc='lower left', fontsize=11,
              framealpha=0.9, handlelength=2.5, handleheight=1.2,
              title='图例', title_fontsize=13)

    # 地图范围 + 网格
    ax.set_extent(config.map_extent, crs=ccrs.PlateCarree())
    gl = ax.gridlines(crs=ccrs.PlateCarree(), draw_labels=True,
        linewidth=0.5, color='#999999', alpha=0.5, linestyle='-')
    gl.top_labels = False
    gl.left_labels = False
    gl.right_labels = True
    gl.xformatter = LONGITUDE_FORMATTER
    gl.yformatter = LATITUDE_FORMATTER
    gl.xlabel_style = {"size": 12, "color": "#333333"}
    gl.ylabel_style = {"size": 12, "color": "#333333"}
    ax.spines["geo"].set_linewidth(1.5)
    ax.spines["geo"].set_edgecolor("#333333")

    # 整体下移 3%
    pos = ax.get_position()
    ax.set_position([pos.x0, pos.y0 - 0.03, pos.width, pos.height])

    # 8. 南海插图
    ax_in = fig.add_axes([0.686, 0.187, 0.25, 0.235], projection=crs)
    ax_in.set_extent(config.inset_extent, crs=ccrs.PlateCarree())

    # 获取 set_extent 后的实际尺寸，对齐到主图右下角
    inset_pos = ax_in.get_position()
    inset_width = inset_pos.x1 - inset_pos.x0
    inset_height = inset_pos.y1 - inset_pos.y0

    margin = 0.005
    main_pos = ax.get_position()
    new_x0 = main_pos.x1 - inset_width - margin
    new_y0 = main_pos.y0 + margin
    ax_in.set_position([new_x0, new_y0, inset_width, inset_height])

    _draw_regions(ax_in, md, prep)
    _draw_boundaries(ax_in, prep, lw=(1.0, 0.6, 1.2))
    _draw_shadows(ax_in, prep)
    ax_in.spines["geo"].set_zorder(999)
    ax_in.spines["geo"].set_linewidth(1.5)
    ax_in.spines["geo"].set_edgecolor("#333333")

    # 9. 保存
    plt.suptitle(title, fontsize=30, fontweight='bold', y=0.93)
    plt.savefig(output, dpi=config.dpi)
    print(f"\n已保存: {output}")
    plt.show()


if __name__ == '__main__':
    main()
