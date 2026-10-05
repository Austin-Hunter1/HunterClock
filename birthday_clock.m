% =========================================================================
% Birthday Clock — Distance Clock Face Visualization
% Maps geographical distances into logarithmic space around a clock face.
%   12:00 = Home Location
%    6:00 = Antipode of the Earth (~20,015 km)
% =========================================================================

clear; clc; close all;

%% 1. Define Locations
% Home: Minnetonka, MN (also where Minnetonka High School is located)
home_lat = 44.9109;
home_lon = -93.5017;

% Family members and their locations (Lat, Lon).
% Stored as a table so each name always stays paired with its own
% coordinates (safer than three separate parallel arrays).
people = table( ...
    {'Becky'; 'Elijah'; 'Caleb'; 'Austin'; 'Micah'; 'Evan'}, ...
    [44.9109;  44.9537;  41.8661;  40.0150;  44.5646;  52.2297], ...
    [-93.5017; -93.0900; -88.1070; -105.2705; -123.2620; 21.0122], ...
    'VariableNames', {'Name', 'Lat', 'Lon'});
% Becky: near home (Minnetonka High School, MN)
% Elijah: St. Paul, MN
% Caleb: Wheaton, IL
% Austin: Boulder, CO
% Micah: Corvallis, OR
% Evan: Warsaw, Poland

num_people = height(people);

%% 2. Calculate Distances (Haversine Formula, vectorized)
R_earth = 6371;            % Earth's radius in kilometers
max_dist_km = pi * R_earth; % Maximum possible distance on Earth (antipode)

lat1 = deg2rad(home_lat);
lon1 = deg2rad(home_lon);
lat2 = deg2rad(people.Lat);
lon2 = deg2rad(people.Lon);

dlat = lat2 - lat1;
dlon = lon2 - lon1;
a = sin(dlat/2).^2 + cos(lat1) .* cos(lat2) .* sin(dlon/2).^2;
c = 2 * atan2(sqrt(a), sqrt(1 - a));
people.Distance_km = R_earth * c;

%% 3. Map Distances to Logarithmic Clock Space
% Clamp minimum distance to 1 km to avoid log(0) issues at 'Home'
people.Distance_km(people.Distance_km < 1) = 1;

min_log = log10(1);            % Represents Home (10^0 = 1 km)
max_log = log10(max_dist_km);  % Represents Antipode (~4.3)

% Normalize log distances to a 0-1 scale, then map to degrees where
% 0 = 12:00 and 180 = 6:00 (antipode).
norm_dist = (log10(people.Distance_km) - min_log) / (max_log - min_log);
angles_deg = norm_dist * 360;

%% 4. Generate Visualization
figure('Name', 'Birthday Distance Clock', 'Color', 'w', 'Position', [100, 100, 700, 700]);
ax = polaraxes;
hold(ax, 'on');

% Configure the "Clock Face" layout
ax.ThetaZeroLocation = 'top';      % 0 degrees = 12:00 position
ax.ThetaDir = 'clockwise';         % Angles increase clockwise
ax.ThetaLim = [0 360];             % Full circle

% Only 0-180 degrees (12:00 to 6:00) is ever used, so only label that half.
ax.ThetaTick = 0:30:330;
ax.ThetaTickLabel = {'12', '1', '2', '3', '4', '5', '6', '', '', '', '', ''};

% Radius here is purely a label-separation stagger, not a data-bearing
% axis, so hide the numeric radial ticks to avoid implying otherwise.
ax.RTick = [];
ax.RLim = [0 1.2];

colors = lines(num_people);
radii_lengths = linspace(0.5, 1, num_people); % Stagger so labels don't overlap

for i = 1:num_people
    theta_rad = deg2rad(angles_deg(i));
    r = radii_lengths(i);

    polarplot(ax, [0 theta_rad], [0 r], '-o', ...
        'Color', colors(i, :), 'LineWidth', 2, ...
        'MarkerFaceColor', colors(i, :), 'MarkerSize', 6);

    text(ax, theta_rad, r + 0.08, ...
        sprintf('%s\n%.0f km', people.Name{i}, people.Distance_km(i)), ...
        'Color', colors(i, :), 'FontWeight', 'bold', ...
        'HorizontalAlignment', 'center');
end

title(ax, 'Distance from Home (Minnetonka, MN) — Logarithmic Clock', ...
    'FontSize', 12, 'FontWeight', 'bold');

hold(ax, 'off');
