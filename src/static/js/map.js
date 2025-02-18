    /* ==============================================
    MAP -->
    =============================================== */
        var locations = [
            ['<div class="infobox"><h3 class="title"><a href="#">OUR USA OFFICE</a></h3><span>NEW YORK CITY 2045 / 65</span><span>+90 555 666 77 88</span></div>', -37.801578, 145.060508, 2]
            ];     
            async function initMap() {
              const { Map } = await google.maps.importLibrary("maps");
              const { AdvancedMarkerElement, PinElement } = await google.maps.importLibrary("marker");
        
              var map = new Map(document.getElementById('map'), {
                zoom: 12,
                scrollwheel: false,
                navigationControl: true,
                mapTypeControl: false,
                scaleControl: false,
                draggable: true,
                styles: [
            {
                "featureType": "administrative",
                "elementType": "labels.text.fill",
                "stylers": [
                    {
                        "color": "#444444"
                    }
                ]
            },
            {
                "featureType": "landscape",
                "elementType": "all",
                "stylers": [
                    {
                        "color": "#f2f2f2"
                    }
                ]
            },
            {
                "featureType": "poi",
                "elementType": "all",
                "stylers": [
                    {
                        "visibility": "off"
                    }
                ]
            },
            {
                "featureType": "road",
                "elementType": "all",
                "stylers": [
                    {
                        "saturation": -100
                    },
                    {
                        "lightness": 45
                    }
                ]
            },
            {
                "featureType": "road.highway",
                "elementType": "all",
                "stylers": [
                    {
                        "visibility": "simplified"
                    }
                ]
            },
            {
                "featureType": "road.arterial",
                "elementType": "labels.icon",
                "stylers": [
                    {
                        "visibility": "off"
                    }
                ]
            },
            {
                "featureType": "transit",
                "elementType": "all",
                "stylers": [
                    {
                        "visibility": "off"
                    }
                ]
            },
            {
                "featureType": "water",
                "elementType": "all",
                "stylers": [
                    {
                        "color": "#1976d2"
                    },
                    {
                        "visibility": "on"
                    }
                ]
            }
        ],
                center: new google.maps.LatLng(-37.801578, 145.060508),
                mapId: 'DEMO_MAP_ID'
                });
                var infowindow = new google.maps.InfoWindow();
                var marker, i;
                for (i = 0; i < locations.length; i++) {
                    const pin = new PinElement({
                        background: "#0069ff",
                        borderColor: "#0069ff",
                        glyphColor: "#fff",
                    });
                    marker = new AdvancedMarkerElement({
                        position: new google.maps.LatLng(locations[i][1], locations[i][2]),
                        map,
                        title: "OUR USA OFFICE",
                        content: pin.element,
                    });
              
                    marker.addListener("click", () => {
                      infowindow.setContent(locations[i][0]);
                      infowindow.open(map, marker);
                    });
                }
            }
        
            initMap();
